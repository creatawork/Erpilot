"""rag 构建入口：嵌入政策语料一次并持久化索引（消耗嵌入 token）。

    uv run --package rag python -m rag build            # 写默认 policy_index.json
    uv run --package rag python -m rag build --out x.json
"""

import argparse
from pathlib import Path

from rag.corpus import DEFAULT_INDEX_PATH, load_corpus
from rag.index import PolicyIndex
from rag.openai_embedder import EmbedConfig, OpenAIEmbedder


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="构建政策检索索引")
    parser.add_argument("command", choices=["build"], help="build：嵌入语料并保存索引")
    parser.add_argument("--out", type=Path, default=DEFAULT_INDEX_PATH, help="索引输出路径")
    args = parser.parse_args(argv)

    chunks = load_corpus()
    embedder = OpenAIEmbedder(EmbedConfig.from_env())
    index = PolicyIndex.build(chunks, embedder)
    index.save(args.out)
    print(f"Indexed {len(chunks)} chunks -> {args.out}")


if __name__ == "__main__":
    main()
