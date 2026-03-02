#!/usr/bin/env python3
"""
Visualize the GMU links_to graph at the subdomain level.

Reads backfill_links_to_gmu.jsonl and produces:
1. A static matplotlib chart of top subdomains by edge weight
2. An interactive pyvis HTML graph you can open in a browser
"""

import json
import os
from collections import defaultdict
from urllib.parse import urlparse

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import networkx as nx
from pyvis.network import Network

DATA_FILE = os.path.join(
    os.path.dirname(__file__),
    "..", "data", "backfill_links_to_gmu.jsonl",
)
OUT_DIR = os.path.join(os.path.dirname(__file__), "..", "data", "viz")
os.makedirs(OUT_DIR, exist_ok=True)

# ---------------------------------------------------------------------------
# 1. Parse the JSONL and build a domain-level directed graph
# ---------------------------------------------------------------------------

def extract_subdomain(url: str) -> str | None:
    """Return the hostname (subdomain) from a URL, or None if unparseable."""
    try:
        host = urlparse(url).hostname
        if host:
            return host.lower()
    except Exception:
        pass
    return None


print("Reading JSONL …")
edge_weights = defaultdict(int)   # (src_domain, dst_domain) -> count
domain_page_count = defaultdict(int)  # domain -> number of source pages
total_lines = 0

with open(DATA_FILE) as f:
    for line in f:
        line = line.strip()
        if not line:
            continue
        total_lines += 1
        rec = json.loads(line)
        src = extract_subdomain(rec["source_url"])
        if not src:
            continue
        domain_page_count[src] += 1
        for target_url in rec.get("links_to", []):
            dst = extract_subdomain(target_url)
            if dst and dst != src:          # skip self-links within the same subdomain
                edge_weights[(src, dst)] += 1

print(f"  Parsed {total_lines:,} source pages across {len(domain_page_count):,} subdomains")
print(f"  {len(edge_weights):,} unique cross-subdomain edges\n")

# ---------------------------------------------------------------------------
# 2. Build the NetworkX directed graph
# ---------------------------------------------------------------------------

G = nx.DiGraph()

for (src, dst), w in edge_weights.items():
    G.add_edge(src, dst, weight=w)

# Add page-count as a node attribute
for node in G.nodes():
    G.nodes[node]["pages"] = domain_page_count.get(node, 0)

print(f"Graph: {G.number_of_nodes()} nodes, {G.number_of_edges()} edges\n")

# ---------------------------------------------------------------------------
# 3. Summary stats
# ---------------------------------------------------------------------------

# Top subdomains by number of source pages
print("=== Top 20 subdomains by crawled pages ===")
for domain, count in sorted(domain_page_count.items(), key=lambda x: -x[1])[:20]:
    print(f"  {domain:40s}  {count:>7,} pages")

# Top edges by weight
print("\n=== Top 20 cross-subdomain edges (by link count) ===")
for (src, dst), w in sorted(edge_weights.items(), key=lambda x: -x[1])[:20]:
    print(f"  {src:35s} → {dst:35s}  {w:>8,}")

# In-degree / out-degree
in_deg = dict(G.in_degree(weight="weight"))
out_deg = dict(G.out_degree(weight="weight"))

print("\n=== Top 20 subdomains by weighted in-degree (most linked TO) ===")
for d, deg in sorted(in_deg.items(), key=lambda x: -x[1])[:20]:
    print(f"  {d:40s}  in-degree {deg:>10,}")

print("\n=== Top 20 subdomains by weighted out-degree (most links FROM) ===")
for d, deg in sorted(out_deg.items(), key=lambda x: -x[1])[:20]:
    print(f"  {d:40s}  out-degree {deg:>10,}")

# ---------------------------------------------------------------------------
# 4. Static matplotlib figure — top N subdomains
# ---------------------------------------------------------------------------

TOP_N = 40  # keep the top N nodes by total degree for the static plot

total_deg = {n: in_deg.get(n, 0) + out_deg.get(n, 0) for n in G.nodes()}
top_nodes = sorted(total_deg, key=total_deg.get, reverse=True)[:TOP_N]
H = G.subgraph(top_nodes).copy()

fig, ax = plt.subplots(figsize=(22, 18))
pos = nx.spring_layout(H, k=2.5, iterations=80, seed=42)

# Node sizes proportional to total degree
node_sizes = [max(300, min(5000, total_deg[n] / 50)) for n in H.nodes()]

# Edge widths proportional to weight (capped)
weights = [H[u][v]["weight"] for u, v in H.edges()]
max_w = max(weights) if weights else 1
edge_widths = [0.3 + 4.0 * (w / max_w) for w in weights]
edge_alphas = [0.15 + 0.6 * (w / max_w) for w in weights]

# Draw
nx.draw_networkx_edges(H, pos, ax=ax, width=edge_widths, alpha=0.35,
                       edge_color="steelblue", arrows=True, arrowsize=12,
                       connectionstyle="arc3,rad=0.1")
nx.draw_networkx_nodes(H, pos, ax=ax, node_size=node_sizes,
                       node_color="coral", alpha=0.85, edgecolors="black", linewidths=0.5)

# Labels: shorten long subdomains
labels = {}
for n in H.nodes():
    label = n.replace(".gmu.edu", "").replace(".edu", "")
    if len(label) > 25:
        label = label[:22] + "…"
    labels[n] = label

nx.draw_networkx_labels(H, pos, labels, font_size=7, font_weight="bold", ax=ax)

ax.set_title(f"GMU Subdomain Link Graph — Top {TOP_N} by degree\n"
             f"({G.number_of_nodes()} total subdomains, {total_lines:,} pages)",
             fontsize=16, fontweight="bold")
ax.axis("off")
plt.tight_layout()

static_path = os.path.join(OUT_DIR, "gmu_link_graph_static.png")
fig.savefig(static_path, dpi=150, bbox_inches="tight")
plt.close(fig)
print(f"\nStatic plot saved → {static_path}")

# ---------------------------------------------------------------------------
# 5. Interactive pyvis graph — top N subdomains
# ---------------------------------------------------------------------------

TOP_N_INTERACTIVE = 60

top_nodes_i = sorted(total_deg, key=total_deg.get, reverse=True)[:TOP_N_INTERACTIVE]
I = G.subgraph(top_nodes_i).copy()

net = Network(height="900px", width="100%", directed=True, bgcolor="#1a1a2e",
              font_color="white", notebook=False)
net.barnes_hut(gravity=-8000, central_gravity=0.3, spring_length=200, damping=0.5)

# Color by whether it's a *.gmu.edu subdomain or external
for node in I.nodes():
    pages = domain_page_count.get(node, 0)
    deg = total_deg.get(node, 0)
    size = max(10, min(60, deg / 200))
    is_gmu = node.endswith(".gmu.edu") or node == "gmu.edu"
    color = "#e94560" if is_gmu else "#0f3460"
    label = node.replace(".gmu.edu", "").replace(".edu", "")
    title = f"<b>{node}</b><br>Pages: {pages:,}<br>In-degree: {in_deg.get(node,0):,}<br>Out-degree: {out_deg.get(node,0):,}"
    net.add_node(node, label=label, title=title, size=size, color=color,
                 borderWidth=2, borderWidthSelected=4)

for u, v, d in I.edges(data=True):
    w = d["weight"]
    width = max(0.3, min(8, w / max_w * 8))
    net.add_edge(u, v, value=w, title=f"{w:,} links", color="rgba(255,255,255,0.15)")

interactive_path = os.path.join(OUT_DIR, "gmu_link_graph_interactive.html")
net.write_html(interactive_path)
print(f"Interactive graph saved → {interactive_path}")
print(f"\nOpen the HTML file in a browser for the full interactive experience!")
