#!/usr/bin/env python2
# -*- coding: utf-8 -*-

from __future__ import absolute_import
from __future__ import print_function
from __future__ import unicode_literals

from cms.grading.ScoreType import ScoreTypeGroup


# Dummy function to mark translatable string.
def N_(message):
    return message


class GroupMinTruncation(ScoreTypeGroup):
    """The score of a submission is the sum of the product of the
    minimum of the ranges with the multiplier of that range.

    Parameters are [[m, t, lo, hi, r], ... ].
    t: see ScoreTypeGroup.
    The score is calculated by (((x - lo) / hi) ** r) * m,
        where x is the minimum value among all outcomes
        truncated by the function min(max(minimum, lo), hi).

    """

    def get_public_outcome(self, outcome, unused_parameter):
        """See ScoreTypeGroup."""
        if outcome <= 0.0:
            return N_("Not correct")
        elif outcome >= 1.0:
            return N_("Correct")
        else:
            return N_("Partially correct")

    def reduce(self, outcomes, unused_parameter):
        """See ScoreTypeGroup."""
        lo, hi, r = unused_parameter[2:5]
        d = hi - lo
        minimum = min(outcomes)
        if d == 0:
            if hi <= minimum:
                return 1
            return 0
        x = min(max(minimum, lo), hi)
        return ((float(x - lo) / (hi - lo)) ** r)
