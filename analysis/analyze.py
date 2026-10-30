#!/usr/bin/env python3
import argparse
import os

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RESULTS = os.path.join(REPO, "results")
H2_MAC = "00:00:00:00:00:02"
HEADER_BYTES = 42
SAMPLE_MARGIN_S = 1.0
MODES = ["baseline", "dynamic"]
STYLE = {"baseline": ("tab:blue", "o"), "dynamic": ("tab:orange", "^")}

REQUIRED = ["mode", "test", "rep", "target_pps", "payload_bytes", "sent", "sent_bytes",
            "sender_elapsed_s", "received", "app_rx_bytes", "rx_duration_s",
            "sdn_pkts_start", "sdn_pkts_end", "sdn_interval_s", "t_start", "t_end"]

METRICS = ["sent", "received", "loss_pct", "offered_mbps", "app_mbps", "sdn_pkts", "sdn_mbps",
           "sdn_payload_mbps", "abs_diff_mbps", "rel_diff_pct", "abs_diff_payload_mbps",
           "rel_diff_payload_pct", "pre_switch_loss_pct", "switch_loss_pct"]


def recompute_sdn(runs, flows):
    flows = flows[flows["eth_dst"] == H2_MAC].sort_values("timestamp")
    ts = flows["timestamp"].to_numpy()
    pkts = flows["packet_count"].to_numpy()
    byts = flows["byte_count"].to_numpy()

    out_pkts, out_bytes = [], []
    for t_start, t_end in zip(runs["t_start"], runs["t_end"]):
        i = np.searchsorted(ts, t_start, side="right") - 1
        j = np.searchsorted(ts, t_end + SAMPLE_MARGIN_S, side="left")
        if i < 0 or j >= len(ts) or pkts[j] < pkts[i]:
            out_pkts.append(np.nan)
            out_bytes.append(np.nan)
        else:
            out_pkts.append(pkts[j] - pkts[i])
            out_bytes.append(byts[j] - byts[i])
    return out_pkts, out_bytes


def analyze(runs, flows):
    df = runs.copy()
    df["sdn_pkts_runner"] = df["sdn_pkts_end"] - df["sdn_pkts_start"]
    df["sdn_pkts"], df["sdn_bytes"] = recompute_sdn(df, flows)
    df["sdn_recomputed"] = df["sdn_pkts"] != df["sdn_pkts_runner"]

    df["loss_pct"] = (df["sent"] - df["received"]) / df["sent"] * 100
    df["offered_mbps"] = df["sent_bytes"] * 8 / df["sender_elapsed_s"] / 1e6
    df["app_mbps"] = df["app_rx_bytes"] * 8 / df["rx_duration_s"].replace(0, np.nan) / 1e6
    df["sdn_mbps"] = df["sdn_bytes"] * 8 / df["sdn_interval_s"].replace(0, np.nan) / 1e6
    df["sdn_payload_mbps"] = df["sdn_mbps"] * df["payload_bytes"] / (df["payload_bytes"] + HEADER_BYTES)

    sdn_nonzero = df["sdn_mbps"].where(df["sdn_mbps"] > 0)
    payload_nonzero = df["sdn_payload_mbps"].where(df["sdn_payload_mbps"] > 0)
    df["abs_diff_mbps"] = (df["app_mbps"] - df["sdn_mbps"]).abs()
    df["rel_diff_pct"] = df["abs_diff_mbps"] / sdn_nonzero * 100
    df["abs_diff_payload_mbps"] = (df["app_mbps"] - df["sdn_payload_mbps"]).abs()
    df["rel_diff_payload_pct"] = df["abs_diff_payload_mbps"] / payload_nonzero * 100

    df["pre_switch_loss_pct"] = (df["sent"] - df["sdn_pkts"]) / df["sent"] * 100
    df["switch_loss_pct"] = (df["sdn_pkts"] - df["received"]) / df["sent"] * 100

    issues = []
    for _, r in df.iterrows():
        problems = []
        if r["received"] > r["sent"]:
            problems.append("received>sent")
        if pd.isna(r["sdn_pkts"]):
            problems.append("no_sdn_samples")
        elif r["sdn_pkts"] < r["received"]:
            problems.append("switch<received")
        elif r["sdn_pkts"] > r["sent"]:
            problems.append("switch>sent")
        if pd.isna(r["app_mbps"]):
            problems.append("no_rx_duration")
        issues.append(";".join(problems))
    df["issues"] = issues
    return df


def summarize(df):
    keys = ["mode", "test", "target_pps", "payload_bytes"]
    grouped = df.groupby(keys)[METRICS].agg(["mean", "std"])
    grouped.columns = [f"{m}_{s}" for m, s in grouped.columns]
    grouped["runs"] = df.groupby(keys).size()
    return grouped.reset_index().round(4)


def save(fig, path):
    fig.tight_layout()
    fig.savefig(path, dpi=150)
    plt.close(fig)


def plot_vs_rate(summary, metric, ylabel, title, path, capacity=False):
    fig, ax = plt.subplots(figsize=(8, 5))
    for mode in MODES:
        color, marker = STYLE[mode]
        s = summary[summary["mode"] == mode]
        main = s[s["payload_bytes"] == 1024].sort_values("target_pps")
        ax.errorbar(main["target_pps"], main[f"{metric}_mean"], yerr=main[f"{metric}_std"],
                    color=color, marker=marker, capsize=4, label=f"{mode} (1024 B, tests A-D)")
        side = s[s["payload_bytes"] != 1024]
        ax.errorbar(side["target_pps"], side[f"{metric}_mean"], yerr=side[f"{metric}_std"],
                    color=color, marker="s", linestyle="none", capsize=4, mfc="white",
                    label=f"{mode} (512 B, test E)")
    if capacity:
        offered = summary[(summary["mode"] == "baseline") & (summary["payload_bytes"] == 1024)]
        offered = offered.sort_values("target_pps")
        ax.plot(offered["target_pps"], offered["offered_mbps_mean"], color="grey",
                linestyle="--", label="Offered load (sender)")
        ax.axhline(10, color="red", linestyle=":", label="Link capacity (10 Mbps)")
    ax.set_xlabel("Target sending rate (packets/s)")
    ax.set_ylabel(ylabel)
    ax.set_title(title)
    ax.grid(alpha=0.3)
    ax.legend()
    save(fig, path)


def plot_app_vs_sdn(df, path):
    fig, ax = plt.subplots(figsize=(7, 6))
    for mode in MODES:
        color, marker = STYLE[mode]
        d = df[df["mode"] == mode]
        ax.scatter(d["sdn_mbps"], d["app_mbps"], color=color, marker=marker, label=mode, alpha=0.8)
    top = np.nanmax([df["sdn_mbps"].max(), df["app_mbps"].max()]) * 1.05
    ax.plot([0, top], [0, top], color="grey", linestyle="--", label="y = x (perfect agreement)")
    ax.set_xlim(0, top)
    ax.set_ylim(0, top)
    ax.set_xlabel("SDN switch throughput (Mbps, includes 42 B headers)")
    ax.set_ylabel("Application throughput at receiver (Mbps, payload only)")
    ax.set_title("Application vs SDN throughput (each point = one run)")
    ax.grid(alpha=0.3)
    ax.legend()
    save(fig, path)


def plot_baseline_vs_dynamic(summary, path):
    tests = sorted(summary["test"].unique())
    labels = []
    for t in tests:
        row = summary[summary["test"] == t].iloc[0]
        labels.append(f"{t}\n{int(row['target_pps'])} pps\n{int(row['payload_bytes'])} B")
    x = np.arange(len(tests))
    width = 0.38

    fig, (ax1, ax2) = plt.subplots(1, 2, figsize=(12, 5))
    for k, mode in enumerate(MODES):
        color, _ = STYLE[mode]
        s = summary[summary["mode"] == mode].set_index("test").reindex(tests)
        offset = (k - 0.5) * width
        ax1.bar(x + offset, s["app_mbps_mean"], width, yerr=s["app_mbps_std"], capsize=3,
                color=color, label=mode)
        ax2.bar(x + offset, s["loss_pct_mean"], width, yerr=s["loss_pct_std"], capsize=3,
                color=color, label=mode)
    ax1.axhline(8, color="red", linestyle=":", label="Threshold / meter (8 Mbps)")
    ax1.set_ylabel("Application throughput (Mbps)")
    ax1.set_title("Received throughput")
    ax2.set_ylabel("Packet loss (%)")
    ax2.set_title("Packet loss")
    for ax in (ax1, ax2):
        ax.set_xticks(x)
        ax.set_xticklabels(labels)
        ax.grid(axis="y", alpha=0.3)
        ax.legend()
    fig.suptitle("Baseline vs dynamic SDN (mean ± std over repetitions)")
    save(fig, path)


def main():
    parser = argparse.ArgumentParser(description="Analyse UDP/SDN bandwidth experiments")
    parser.add_argument("--measurements", default=os.path.join(RESULTS, "measurements.csv"))
    parser.add_argument("--flows", default=os.path.join(RESULTS, "flow_stats.csv"))
    parser.add_argument("--outdir", default=RESULTS)
    args = parser.parse_args()

    runs = pd.read_csv(args.measurements)
    missing = [c for c in REQUIRED if c not in runs.columns]
    if missing:
        raise SystemExit(f"measurements.csv is missing columns: {missing}")
    runs = runs.dropna(subset=["sent", "t_start", "t_end"])
    runs = runs[runs["sent"] > 0]
    flows = pd.read_csv(args.flows)

    df = analyze(runs, flows)
    summary = summarize(df)

    graphs = os.path.join(args.outdir, "graphs")
    os.makedirs(graphs, exist_ok=True)
    df.round(6).to_csv(os.path.join(args.outdir, "analyzed_results.csv"), index=False)
    summary.to_csv(os.path.join(args.outdir, "summary_results.csv"), index=False)

    plot_vs_rate(summary, "app_mbps", "Application throughput (Mbps)",
                 "Traffic rate vs application throughput",
                 os.path.join(graphs, "throughput_vs_rate.png"), capacity=True)
    plot_vs_rate(summary, "loss_pct", "Packet loss (%)", "Traffic rate vs packet loss",
                 os.path.join(graphs, "packet_loss_vs_rate.png"))
    plot_app_vs_sdn(df, os.path.join(graphs, "application_vs_sdn.png"))
    plot_baseline_vs_dynamic(summary, os.path.join(graphs, "baseline_vs_dynamic.png"))

    print(f"Runs analysed: {len(df)}")
    print(f"SDN counters recomputed (sampling-timing fix): {int(df['sdn_recomputed'].sum())}")
    flagged = df[df["issues"] != ""]
    print(f"Runs with validation issues: {len(flagged)}")
    for _, r in flagged.iterrows():
        print(f"  {r['mode']} {r['test']} r{r['rep']}: {r['issues']}")

    cols = ["mode", "test", "target_pps", "payload_bytes", "loss_pct_mean", "app_mbps_mean",
            "sdn_mbps_mean", "sdn_payload_mbps_mean", "rel_diff_payload_pct_mean",
            "pre_switch_loss_pct_mean", "switch_loss_pct_mean"]
    with pd.option_context("display.width", 200, "display.max_columns", None):
        print(summary[cols].to_string(index=False))
    print(f"\nWrote analyzed_results.csv, summary_results.csv and 4 graphs to {args.outdir}")


if __name__ == "__main__":
    main()
