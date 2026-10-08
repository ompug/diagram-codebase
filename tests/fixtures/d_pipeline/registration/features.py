"""Geometric features used to sanity-check the initial alignment."""

import numpy as np


def centroid(points: np.ndarray) -> np.ndarray:
    return points.mean(axis=0)


def compute_features(points: np.ndarray) -> dict:
    """Centroid and principal axes of the cloud."""
    c = centroid(points)
    cov = np.cov((points - c).T)
    _, axes = np.linalg.eigh(cov)
    return {"centroid": c, "axes": axes}
