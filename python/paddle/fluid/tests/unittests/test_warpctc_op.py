#   Copyright (c) 2018 PaddlePaddle Authors. All Rights Reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import sys
import unittest
import numpy as np
from op_test import OpTest
from test_softmax_op import stable_softmax

CUDA_BLOCK_SIZE = 512


class CTCForward(object):
    def __init__(self, softmax, softmax_lod, labels, labels_lod, blank,
                 norm_by_times):
        self.softmax = softmax
        self.softmax_lod = softmax_lod
        assert labels.shape[1] == 1
        self.labels = labels
        self.labels_lod = labels_lod
        self.blank = blank
        self.norm_by_times = norm_by_times

        self.level = 0
        self.num_classes = softmax.shape[1]
        self.batch_size = len(softmax_lod[self.level]) - 1
        assert self.batch_size == len(labels_lod[self.level]) - 1

        self.loss = np.zeros([self.batch_size, 1], dtype="float32")
        self.gradient = np.zeros(self.softmax.shape, dtype="float32")

        # float64
        self.EXP_MAX = sys.float_info.max
        self.EXP_MIN = sys.float_info.min
        self.LOG_ZERO = np.log(self.EXP_MIN)
        self.LOG_INFINITY = np.log(self.EXP_MAX)

    def safe_exp(self, x):
        if x <= self.LOG_ZERO:
            return 0.0
        if x >= self.LOG_INFINITY:
            return self.EXP_MAX
        return np.exp(x)

    def safe_log(self, x):
        if x <= self.EXP_MIN:
            return self.LOG_ZERO
        return np.log(x)

    # x = lna and y = lnb are in log scale, ln(a / b) = lna - lnb
    def log_div(self, x, y):
        res = x - y
        if res <= self.LOG_ZERO:
            return self.LOG_ZERO
        if res >= self.LOG_INFINITY:
            return self.LOG_INFINITY
        return res

    # x = lna and y = lnb are in log scale, ln(a * b) = lna + lnb
    def log_mul(self, x, y):
        res = x + y
        if res <= self.LOG_ZERO:
            return self.LOG_ZERO
        if res >= self.LOG_INFINITY:
            return self.LOG_INFINITY
        return res

    # x = lna and y = lnb are in log scale,
    # ln(a + b) = lna + ln(1 + exp(lnb - lna)), where b > a
    def log_add(self, x, y):
        if x < y:
            t = y
            y = x
            x = t
        return x + self.safe_log(1 + self.safe_exp(y - x))

    def segment_range(self, time, total_times, total_segments):
        start = max(0, total_segments - (2 * (total_times - time)))
        end = min(total_segments, 2 * (time + 1))
        return start, end

    def forward_a_sequence(self, softmax_a_sequence, labels_a_sequence):
        total_times = softmax_a_sequence.shape[0]
        total_segments = labels_a_sequence.shape[0] * 2 + 1

        required_times = labels_a_sequence.shape[0]
        old_label = -1
        for i in range(labels_a_sequence.shape[0]):
            # two contingous labels with the same value
            if labels_a_sequence[i, 0] == old_label:
                required_times = required_times + 1
            old_label = labels_a_sequence[i, 0]

        if total_times < required_times:
            return 0

        # calculate the forward and backward variables,
        # reference Chapter 7.3 of "Alex Grave, Supervised Sequence
        # Labelling with Recurrent Neural Networks"
        log_acts = np.zeros([total_times, self.num_classes], dtype="float32")
        for i in range(total_times):
            for j in range(self.num_classes):
                log_acts[i, j] = self.safe_log(softmax_a_sequence[i, j])

        # calculate the forward variables
        forward_vars = np.zeros([total_times, total_segments], dtype="float32")
        for i in range(total_times):
            for j in range(total_segments):
                forward_vars[i, j] = self.LOG_ZERO

        for i in range(total_times):
            # dp initialization at t0
            if i == 0:
                forward_vars[i, 0] = log_acts[0, self.blank]
                if total_segments > 1:
                    forward_vars[i, 1] = log_acts[0, labels_a_sequence[i, 0]]
                continue

            # dp from t1
            start, end = self.segment_range(i, total_times, total_segments)
            for k in range(end - start):
                j = k + start
                if j & 1 == 1:
                    label_idx = j / 2
                    label_val = labels_a_sequence[label_idx, 0]
                    fv = self.log_add(forward_vars[i - 1, j],
                                      forward_vars[i - 1, j - 1])
                    if j > 1 and label_val != labels_a_sequence[label_idx - 1,
                                                                0]:
                        fv = self.log_add(fv, forward_vars[i - 1, j - 2])
                    fv = self.log_mul(fv, log_acts[i, label_val])
                else:
                    fv = forward_vars[i - 1, j]
                    if j > 0:
                        fv = self.log_add(fv, forward_vars[i - 1, j - 1])
                    fv = self.log_mul(fv, log_acts[i, self.blank])
                forward_vars[i, j] = fv

        # sum the last two value as log_prob
        log_prob = forward_vars[total_times - 1, total_segments - 1]
        if total_segments > 1:
            log_prob = self.log_add(
                log_prob, forward_vars[total_times - 1, total_segments - 2])

        return -log_prob

    def forward(self):
        for i in range(self.batch_size):
            softmax_start_i = self.softmax_lod[self.level][i]
            softmax_end_i = self.softmax_lod[self.level][i + 1]
            labels_start_i = self.labels_lod[self.level][i]
            labels_end_i = self.labels_lod[self.level][i + 1]

            softmax_a_sequence = self.softmax[softmax_start_i:softmax_end_i, :]
            labels_a_sequence = self.labels[labels_start_i:labels_end_i, :]
            self.loss[i] = self.forward_a_sequence(softmax_a_sequence,
                                                   labels_a_sequence)
        return self.loss


class TestWarpCTCOp(OpTest):
    def config(self):
        self.batch_size = 4
        self.num_classes = 8
        self.logits_lod = [[0, 4, 5, 8, 11]]
        self.labels_lod = [[0, 3, 4, 8, 12]]
        self.blank = self.num_classes - 1
        self.norm_by_times = False

    def setUp(self):
        self.op_type = "warpctc"
        self.config()

        logits = np.random.uniform(
            0.1, 1.0,
            [self.logits_lod[0][-1], self.num_classes]).astype("float32")
        softmax = np.apply_along_axis(stable_softmax, 1, logits)
        # labels should not be blank
        labels = np.random.randint(
            0, self.num_classes - 1, [self.labels_lod[0][-1], 1], dtype="int32")

        ctc = CTCForward(softmax, self.logits_lod, labels, self.labels_lod,
                         self.blank, self.norm_by_times)
        loss = ctc.forward()

        max_sequence_length = 0
        for i in range(self.batch_size):
            max_sequence_length = max(
                max_sequence_length,
                self.logits_lod[0][i + 1] - self.logits_lod[0][i])
        self.gradient = np.zeros(
            [max_sequence_length, self.batch_size, self.num_classes],
            dtype="float32")

        self.inputs = {
            "Logits": (logits, self.logits_lod),
            "Label": (labels, self.labels_lod)
        }
        self.outputs = {"Loss": loss}
        self.attrs = {"blank": self.blank, "norm_by_times": self.norm_by_times}

    def test_check_output(self):
        self.check_output()

    def test_check_grad(self):
        self.outputs['WarpCTCGrad'] = self.gradient
        self.check_grad(["Logits"], "Loss", max_relative_error=0.007)


class TestWarpCTCOpCase1(TestWarpCTCOp):
    def config(self):
        self.batch_size = 4
        self.num_classes = CUDA_BLOCK_SIZE + 2
        self.logits_lod = [[0, 4, 5, 8, 11]]
        self.labels_lod = [[0, 3, 4, 8, 12]]
        self.blank = 0
        self.norm_by_times = False


if __name__ == "__main__":
    unittest.main()
