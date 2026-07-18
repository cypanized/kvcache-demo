#!/usr/bin/env python3
"""Architecture figure: vLLM / LMCache / NIXL / storage — VAST doc styling."""
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.patches import FancyArrowPatch, FancyBboxPatch

DEEP = "#1E3A5F"
LIGHT = "#F0F7FA"
GRAY_BG = "#F5F5F5"
BORDER = "#B9C4CE"
INK = "#22303C"
MUT = "#5F7183"
CYAN = "#0A87B0"

fig, ax = plt.subplots(figsize=(9.6, 7.0), dpi=200)
ax.set_xlim(0, 960)
ax.set_ylim(0, 700)
ax.axis("off")
ax.invert_yaxis()


def box(x, y, w, h, fill, edge=BORDER, lw=1.0, r=8):
    ax.add_patch(FancyBboxPatch((x, y), w, h, boxstyle=f"round,pad=0,rounding_size={r}",
                                facecolor=fill, edgecolor=edge, linewidth=lw))


def txt(x, y, s, size=11, color=INK, weight="normal", ha="center", family="sans-serif"):
    ax.text(x, y, s, size=size, color=color, weight=weight, ha=ha, va="center",
            family=family, linespacing=1.35)


def arrow(x1, y1, x2, y2, color=DEEP, lw=1.6, style="-|>", dashed=False, curve=0.0):
    ax.add_patch(FancyArrowPatch((x1, y1), (x2, y2), arrowstyle=style, mutation_scale=13,
                                 color=color, lw=lw, shrinkA=2, shrinkB=2,
                                 linestyle=(0, (4, 3)) if dashed else "solid",
                                 connectionstyle=f"arc3,rad={curve}"))


# ---------- client ----------
box(330, 20, 300, 52, LIGHT, DEEP, 1.2)
txt(480, 38, "Client", 12, DEEP, "bold")
txt(480, 58, "OpenAI-compatible API — chat/completions", 9, MUT)

arrow(480, 72, 480, 106)
txt(575, 88, "prompts / streamed tokens", 8.5, MUT, ha="left")

# ---------- vLLM ----------
box(120, 106, 720, 148, "white", DEEP, 1.6)
txt(150, 128, "vLLM — inference engine", 12.5, DEEP, "bold", ha="left")
box(150, 148, 200, 84, GRAY_BG)
txt(250, 172, "Scheduler +", 10.5, INK, "bold")
txt(250, 190, "model forward pass", 10.5, INK, "bold")
txt(250, 212, "prefill & decode", 9, MUT)
box(380, 148, 220, 84, LIGHT, CYAN, 1.2)
txt(490, 172, "Paged KV cache", 10.5, DEEP, "bold")
txt(490, 192, "GPU HBM · tier 0", 9.5, CYAN, "bold")
txt(490, 212, "capped: 74,880 tokens", 9, MUT)
box(630, 148, 180, 84, GRAY_BG)
txt(720, 172, "KV connector", 10.5, INK, "bold")
txt(720, 192, "LMCacheConnectorV1", 8.5, MUT, family="monospace")
txt(720, 212, "--kv-transfer-config", 8.5, MUT, family="monospace")

arrow(600, 190, 630, 190, CYAN, 1.4, style="<|-|>")

# vLLM <-> LMCache
arrow(680, 254, 680, 292)
arrow(760, 292, 760, 254)
txt(605, 273, "store KV", 8.5, DEEP)
txt(840, 273, "retrieve KV", 8.5, DEEP)

# ---------- LMCache ----------
box(120, 292, 720, 168, "white", DEEP, 1.6)
txt(150, 314, "LMCache — KV cache manager", 12.5, DEEP, "bold", ha="left")
box(150, 336, 200, 102, GRAY_BG)
txt(250, 360, "Token chunking", 10.5, INK, "bold")
txt(250, 380, "256-token chunks", 9, MUT)
txt(250, 398, "hash → cache key", 9, MUT)
txt(250, 418, "≈48 MB per chunk", 9, MUT)
box(380, 336, 170, 102, GRAY_BG)
txt(465, 360, "Storage manager", 10.5, INK, "bold")
txt(465, 380, "lookup, LRU eviction", 9, MUT)
txt(465, 398, "async writes", 9, MUT)
box(580, 336, 110, 102, LIGHT, CYAN, 1.2)
txt(635, 360, "CPU RAM", 10, DEEP, "bold")
txt(635, 380, "tier 1", 9.5, CYAN, "bold")
txt(635, 400, "pinned pool", 8.5, MUT)
txt(635, 418, "local_cpu", 8, MUT, family="monospace")
box(700, 336, 110, 102, LIGHT, CYAN, 1.2)
txt(755, 360, "NIXL storage", 10, DEEP, "bold")
txt(755, 380, "backend", 10, DEEP, "bold")
txt(755, 400, "tier 2 gateway", 8.5, CYAN, "bold")
txt(755, 418, "extra_config", 8, MUT, family="monospace")

arrow(350, 387, 380, 387, MUT, 1.2)
arrow(550, 387, 580, 387, MUT, 1.2)
arrow(690, 387, 700, 387, MUT, 1.2)

# LMCache -> NIXL
arrow(755, 460, 755, 498)
arrow(835, 498, 835, 460, curve=0.0)
txt(655, 479, "staged via CPU buffer", 8.5, DEEP)
txt(900, 479, "reads", 8.5, DEEP)

# ---------- NIXL ----------
box(120, 498, 720, 92, "white", DEEP, 1.6)
txt(150, 520, "NVIDIA NIXL — transfer library", 12.5, DEEP, "bold", ha="left")
box(150, 540, 240, 40, GRAY_BG)
txt(270, 560, "NIXL agent · descriptor registration", 9.5, INK)
box(420, 540, 130, 40, LIGHT, CYAN, 1.2)
txt(485, 554, "POSIX plugin", 10, DEEP, "bold")
txt(485, 571, "used in this demo", 8, CYAN)
box(570, 540, 120, 40, GRAY_BG)
txt(630, 554, "GDS / GDS_MT", 9.5, INK)
txt(630, 571, "GPUDirect Storage", 8, MUT)
box(710, 540, 100, 40, GRAY_BG)
txt(760, 554, "OBJ", 9.5, INK)
txt(760, 571, "S3-compatible", 8, MUT)

arrow(390, 560, 420, 560, MUT, 1.2)

# NIXL -> storage
arrow(485, 590, 485, 626)

# ---------- storage ----------
box(120, 626, 720, 58, LIGHT, DEEP, 1.4)
txt(150, 644, "External storage — tier 2", 11.5, DEEP, "bold", ha="left")
txt(150, 666, "Demo: NVMe file pool (nixl_pool_size slots × 48 MB chunk files)", 9.5, INK, ha="left")
txt(830, 655, "Production: VAST NFS / GDS mount\ncapacity decoupled from the GPU node", 9, MUT, ha="right")

plt.tight_layout(pad=0.4)
plt.savefig("/Users/wongtran/kvcache/report/architecture.png", dpi=200,
            bbox_inches="tight", facecolor="white")
print("saved architecture.png")
