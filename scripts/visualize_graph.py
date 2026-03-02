#!/usr/bin/env python3
"""
Visualize the links_to graph from the backfill JSONL file.

Generates:
  1. Subdomain-level graph (interactive HTML) — overview of how subdomains connect
  2. Stats: top pages by in-degree, out-degree, and subdomain breakdown

Usage:
    python3 scripts/visualize_graph.py --university-id gmu
    python3 scripts/visualize_graph.py --university-id gmu --subdomain chhs.gmu.edu
"""

import argparse
import json
import os
from collections import defaultdict
from urllib.parse import urlparse

import networkx as nx
from pyvis.network import Network

RESULTS_DIR = os.path.join(os.path.dirname(__file__), '..', 'data')


def load_links(university_id):
    """Load links_to from local JSONL file."""
    jsonl_path = os.path.join(RESULTS_DIR, f'backfill_links_to_{university_id}.jsonl')
    forward_map = {}
    with open(jsonl_path, 'r') as f:
        for line in f:
            if not line.strip():
                continue
            record = json.loads(line)
            forward_map[record['source_url']] = record['links_to']
    return forward_map


def get_subdomain(url):
    parsed = urlparse(url)
    return (parsed.hostname or '').lower()


def build_subdomain_graph(forward_map):
    """Aggregate page-level graph into subdomain-level graph."""
    subdomain_edges = defaultdict(int)
    subdomain_pages = defaultdict(int)

    for source_url, targets in forward_map.items():
        src_sub = get_subdomain(source_url)
        subdomain_pages[src_sub] += 1
        for target in targets:
            tgt_sub = get_subdomain(target)
            subdomain_edges[(src_sub, tgt_sub)] += 1

    G = nx.DiGraph()
    for sub, count in subdomain_pages.items():
        G.add_node(sub, page_count=count)

    for (src, tgt), weight in subdomain_edges.items():
        if src != tgt:  # skip self-loops for cleaner viz
            G.add_edge(src, tgt, weight=weight)

    # Add self-loop counts as node attribute
    for (src, tgt), weight in subdomain_edges.items():
        if src == tgt and src in G.nodes:
            G.nodes[src]['internal_links'] = weight

    return G


def build_page_graph(forward_map, subdomain=None):
    """Build page-level graph, optionally filtered to a subdomain."""
    G = nx.DiGraph()
    for source_url, targets in forward_map.items():
        if subdomain and get_subdomain(source_url) != subdomain:
            continue
        G.add_node(source_url)
        for target in targets:
            if subdomain and get_subdomain(target) != subdomain:
                continue
            G.add_edge(source_url, target)
    return G


def visualize_subdomain(G, university_id):
    """Create interactive subdomain-level visualization."""
    net = Network(height='900px', width='100%', directed=True,
                  bgcolor='#1a1a2e', font_color='white')
    net.barnes_hut(gravity=-3000, central_gravity=0.3, spring_length=200)

    # Scale node sizes by page count
    max_pages = max((d.get('page_count', 1) for _, d in G.nodes(data=True)), default=1)

    for node, data in G.nodes(data=True):
        pages = data.get('page_count', 0)
        internal = data.get('internal_links', 0)
        in_deg = G.in_degree(node, weight='weight')
        out_deg = G.out_degree(node, weight='weight')
        size = max(10, 60 * (pages / max_pages))

        title = (f"<b>{node}</b><br>"
                 f"Pages: {pages}<br>"
                 f"Internal links: {internal}<br>"
                 f"Links from other subdomains: {in_deg}<br>"
                 f"Links to other subdomains: {out_deg}")

        net.add_node(node, label=node, size=size, title=title,
                     color='#e94560' if pages > 100 else '#0f3460')

    for src, tgt, data in G.edges(data=True):
        weight = data.get('weight', 1)
        width = max(0.5, min(8, weight / 100))
        net.add_edge(src, tgt, value=weight, title=f"{weight} links",
                     color='#533483' if weight < 50 else '#e94560')

    out_path = os.path.join(RESULTS_DIR, f'graph_subdomains_{university_id}.html')
    net.write_html(out_path)
    return out_path


def visualize_pages(G, university_id, subdomain):
    """Create interactive page-level visualization for a subdomain."""
    # Limit to top 200 nodes by degree for readability
    if len(G.nodes) > 200:
        degrees = dict(G.degree())
        top_nodes = sorted(degrees, key=degrees.get, reverse=True)[:200]
        G = G.subgraph(top_nodes).copy()

    net = Network(height='900px', width='100%', directed=True,
                  bgcolor='#1a1a2e', font_color='white')
    net.barnes_hut(gravity=-2000, central_gravity=0.3, spring_length=150)

    max_deg = max(dict(G.degree()).values(), default=1)

    for node in G.nodes():
        deg = G.degree(node)
        in_deg = G.in_degree(node)
        out_deg = G.out_degree(node)
        size = max(8, 40 * (deg / max_deg))

        # Shorten label to path only
        path = urlparse(node).path or '/'
        label = path[:40] + '...' if len(path) > 40 else path

        title = (f"<b>{node}</b><br>"
                 f"In-degree: {in_deg}<br>"
                 f"Out-degree: {out_deg}")

        net.add_node(node, label=label, size=size, title=title,
                     color='#e94560' if in_deg > 20 else '#0f3460')

    for src, tgt in G.edges():
        net.add_edge(src, tgt, color='#533483')

    safe_sub = subdomain.replace('.', '_')
    out_path = os.path.join(RESULTS_DIR, f'graph_pages_{university_id}_{safe_sub}.html')
    net.write_html(out_path)
    return out_path


def print_stats(forward_map):
    """Print graph statistics."""
    # Compute reverse map
    reverse_map = defaultdict(list)
    for source, targets in forward_map.items():
        for target in targets:
            reverse_map[target].append(source)

    # Subdomain breakdown
    subdomain_counts = defaultdict(int)
    for url in forward_map:
        subdomain_counts[get_subdomain(url)] += 1

    print(f"\n{'='*60}")
    print("GRAPH STATISTICS")
    print(f"{'='*60}")
    print(f"  Pages with outgoing links:  {len(forward_map)}")
    print(f"  Unique target URLs:         {len(reverse_map)}")
    print(f"  Total edges:                {sum(len(v) for v in forward_map.values())}")
    print(f"  Subdomains:                 {len(subdomain_counts)}")

    print(f"\n  Top subdomains by page count:")
    for sub, count in sorted(subdomain_counts.items(), key=lambda x: -x[1])[:15]:
        print(f"    {count:>6} pages  {sub}")

    print(f"\n  Top pages by IN-degree (most linked to):")
    top_in = sorted(reverse_map.items(), key=lambda x: -len(x[1]))[:15]
    for url, sources in top_in:
        print(f"    {len(sources):>5} ←  {url[:70]}")

    print(f"\n  Top pages by OUT-degree (most outgoing links):")
    top_out = sorted(forward_map.items(), key=lambda x: -len(x[1]))[:15]
    for url, targets in top_out:
        print(f"    {len(targets):>5} →  {url[:70]}")

    print(f"{'='*60}")


def main():
    parser = argparse.ArgumentParser(description='Visualize links_to graph')
    parser.add_argument('--university-id', required=True)
    parser.add_argument('--subdomain', default=None,
                        help='Visualize page-level graph for a specific subdomain')
    args = parser.parse_args()

    os.makedirs(RESULTS_DIR, exist_ok=True)

    print(f"Loading links_to for {args.university_id}...")
    forward_map = load_links(args.university_id)
    print(f"  Loaded {len(forward_map)} pages with links")

    print_stats(forward_map)

    if args.subdomain:
        print(f"\nBuilding page-level graph for {args.subdomain}...")
        G = build_page_graph(forward_map, args.subdomain)
        print(f"  Nodes: {len(G.nodes)}, Edges: {len(G.edges)}")
        path = visualize_pages(G, args.university_id, args.subdomain)
        print(f"\n  Visualization: {path}")
    else:
        print(f"\nBuilding subdomain-level graph...")
        G = build_subdomain_graph(forward_map)
        print(f"  Nodes: {len(G.nodes)}, Edges: {len(G.edges)}")
        path = visualize_subdomain(G, args.university_id)
        print(f"\n  Visualization: {path}")

    print(f"  Open in browser to explore interactively.")


if __name__ == '__main__':
    main()
