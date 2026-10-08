"""Command-line entry point for the registration pipeline."""

import yaml

from registration import features, icp, io, preprocess


def main() -> None:
    with open("config.yaml") as fh:
        cfg = yaml.safe_load(fh)
    source = io.load_cloud(cfg["source"])
    target = io.load_cloud(cfg["target"])
    source = preprocess.voxel_downsample(source, cfg["voxel_size"])
    target = preprocess.voxel_downsample(target, cfg["voxel_size"])
    source = preprocess.remove_outliers(source, cfg["outlier_k"], cfg["outlier_std"])
    target = preprocess.remove_outliers(target, cfg["outlier_k"], cfg["outlier_std"])
    feats = features.compute_features(source)
    print("source centroid", feats["centroid"])
    transform, rmse = icp.register(source, target, cfg["icp"]["max_iters"], cfg["icp"]["tol"])
    io.save_result(cfg["output"], transform, rmse)


if __name__ == "__main__":
    main()
