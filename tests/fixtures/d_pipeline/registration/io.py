"""Reading point clouds and writing registration results."""

import numpy as np


def load_cloud(path: str) -> np.ndarray:
    """Load an N x 3 XYZ point cloud from a whitespace-separated text file."""
    return np.loadtxt(path, dtype=np.float64)[:, :3]


def save_result(path: str, transform: np.ndarray, rmse: float) -> None:
    np.savetxt(path, transform, header=f"rmse={rmse:.6f}")
