"""Point-cloud clean-up before registration."""

import numpy as np


def voxel_downsample(points: np.ndarray, voxel_size: float) -> np.ndarray:
    """Keep one centroid per occupied voxel."""
    keys = np.floor(points / voxel_size).astype(np.int64)
    _, inverse = np.unique(keys, axis=0, return_inverse=True)
    counts = np.bincount(inverse)
    sums = np.zeros((counts.size, 3))
    np.add.at(sums, inverse, points)
    return sums / counts[:, None]


def remove_outliers(points: np.ndarray, k: int = 8, std_ratio: float = 2.0) -> np.ndarray:
    """Statistical outlier removal on mean k-nearest-neighbour distance."""
    d = np.linalg.norm(points[:, None, :] - points[None, :, :], axis=2)
    knn = np.sort(d, axis=1)[:, 1 : k + 1].mean(axis=1)
    keep = knn < knn.mean() + std_ratio * knn.std()
    return points[keep]
