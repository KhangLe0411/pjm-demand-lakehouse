"""`gate_score` is the whole basis of the promotion decision.

It exists as one function precisely so the candidate and the incumbent cannot be scored
by two different code paths — which is how the gate came to compare numbers measured on
different holdouts (D-43). These tests hold that property in place.
"""
import numpy as np
import pandas as pd
import pytest

from src.ml.evaluate import gate_score
from src.ml.models import LABEL
from src.ml.registry import GATE_METRIC

FEATS = ["f1", "f2", "f3"]


def frame(n=300, seed=0, scale=1.0):
    rng = np.random.default_rng(seed)
    f1, f2 = rng.normal(100, 10, n), rng.normal(50, 5, n)
    df = pd.DataFrame({"f1": f1, "f2": f2, "f3": rng.normal(0, 1, n)})
    df[LABEL] = (900 * f1 + 40 * f2 + rng.normal(0, 50, n)) * scale
    return df


def test_the_metric_the_gate_decides_on_is_actually_returned():
    """`GATE_METRIC` lives in registry.py and the key is written literally here. If they
    drift apart the notebook raises a KeyError at the moment it decides what to serve —
    a bad place to find out."""
    out = gate_score(frame(seed=1), frame(seed=2), FEATS)
    assert GATE_METRIC in out


def test_scoring_is_deterministic():
    """Two identical calls must agree exactly. If they did not, a candidate could beat
    an incumbent on noise alone and the 2% band would be measuring the sampler."""
    train, scored = frame(seed=1), frame(seed=2)
    first = gate_score(train, scored, FEATS)
    second = gate_score(train, scored, FEATS)
    assert first == second


def test_the_same_holdout_scored_from_two_training_windows_differs():
    """The comparison only says anything if the training window moves the number."""
    scored = frame(seed=2)
    small = gate_score(frame(n=120, seed=1), scored, FEATS)
    large = gate_score(frame(n=600, seed=1), scored, FEATS)
    assert small[GATE_METRIC] != large[GATE_METRIC]
    # Both describe the same rows, which is the point of comparing them at all.
    assert small["gate_holdout_rows"] == large["gate_holdout_rows"] == float(len(scored))


def test_unlabelled_rows_in_the_evaluation_frame_are_refused():
    """A null label would drop out of the mean silently and leave the two sides of the
    comparison with different denominators — the D-43 failure in miniature."""
    scored = frame(seed=2)
    scored.loc[scored.index[:5], LABEL] = np.nan
    with pytest.raises(ValueError, match="unlabelled"):
        gate_score(frame(seed=1), scored, FEATS)


@pytest.mark.parametrize("empty", ["train", "scored"])
def test_empty_frames_are_refused(empty):
    train, scored = frame(seed=1), frame(seed=2)
    if empty == "train":
        train = train.iloc[0:0]
    else:
        scored = scored.iloc[0:0]
    with pytest.raises(ValueError, match="empty"):
        gate_score(train, scored, FEATS)
