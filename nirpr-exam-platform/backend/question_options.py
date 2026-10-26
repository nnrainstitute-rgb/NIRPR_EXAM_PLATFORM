"""Reposition stored option text while retaining alphabetically ordered labels."""
import random


def balanced_correct_labels(count, labels=("A","B","C","D"), rng=None):
    rng = rng or random.SystemRandom()
    targets = [labels[index % len(labels)] for index in range(count)]
    rng.shuffle(targets)
    return targets


def redistribute_options(options, correct_answer, target_label, rng=None):
    rng = rng or random.SystemRandom()
    labels = sorted(options)
    if target_label not in labels or len(correct_answer) != 1 or correct_answer[0] not in options:
        raise ValueError("Expected one valid correct option and a valid target label")
    correct_label = correct_answer[0]
    others = [options[label] for label in labels if label != correct_label]
    rng.shuffle(others)
    remaining = iter(others)
    result = {label: options[correct_label] if label == target_label else next(remaining) for label in labels}
    return result, [target_label]
