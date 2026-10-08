"""Point-to-point Iterative Closest Point registration."""

import numpy as np


def find_correspondences(source: np.ndarray, target: np.ndarray) -> np.ndarray:
    """Index of the nearest target point for every source point (brute-force nearest neighbor)."""
    d = np.linalg.norm(source[:, None, :] - target[None, :, :], axis=2)
    return np.argmin(d, axis=1)


def estimate_transform(source: np.ndarray, target: np.ndarray) -> np.ndarray:
    """Least-squares rigid transform via SVD (Kabsch)."""
    cs, ct = source.mean(axis=0), target.mean(axis=0)
    h = (source - cs).T @ (target - ct)
    u, _, vt = np.linalg.svd(h)
    r = vt.T @ u.T
    if np.linalg.det(r) < 0:
        vt[-1, :] *= -1
        r = vt.T @ u.T
    t = ct - r @ cs
    transform = np.eye(4)
    transform[:3, :3] = r
    transform[:3, 3] = t
    return transform


def apply_transform(points: np.ndarray, transform: np.ndarray) -> np.ndarray:
    return points @ transform[:3, :3].T + transform[:3, 3]


def register(source: np.ndarray, target: np.ndarray, max_iters: int = 50, tol: float = 1e-6):
    """Iteratively align source to target; returns (4x4 transform, rmse)."""
    current = source.copy()
    total = np.eye(4)
    prev_error = np.inf
    error = np.inf
    for _ in range(max_iters):
        idx = find_correspondences(current, target)
        matched = target[idx]
        step = estimate_transform(current, matched)
        current = apply_transform(current, step)
        total = step @ total
        error = float(np.sqrt(np.mean(np.sum((current - matched) ** 2, axis=1))))
        if abs(prev_error - error) < tol:
            break
        prev_error = error
    return total, error
