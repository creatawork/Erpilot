"""rag 构建入口：嵌入政策语料一次并持久化索引（消耗嵌入 token）。

uv run --env-file .env --package rag python -m rag build  # 写默认 policy_index.json
uv run --env-file .env --package rag python -m rag build --out x.json
"""

import argparse
from pathlib import Path
from tempfile import NamedTemporaryFile

from rag.corpus import DEFAULT_INDEX_PATH, load_corpus
from rag.index import PolicyIndex, refresh_index_metadata
from rag.openai_embedder import EmbedConfig, OpenAIEmbedder


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="构建政策检索索引")
    parser.add_argument(
        "command", choices=["build", "refresh"], help="build 嵌入；refresh 核对并更新元数据"
    )
    parser.add_argument("--out", type=Path, default=DEFAULT_INDEX_PATH, help="索引输出路径")
    parser.add_argument(
        "--from-index", type=Path, default=DEFAULT_INDEX_PATH, help="refresh 输入索引"
    )
    args = parser.parse_args(argv)

    chunks = load_corpus()
    if args.command == "refresh":
        index = refresh_index_metadata(PolicyIndex.load(args.from_index), chunks)
    else:
        embedder = OpenAIEmbedder(EmbedConfig.from_env())
        index = PolicyIndex.build(chunks, embedder)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    with NamedTemporaryFile(dir=args.out.parent, delete=False) as stream:
        temporary = Path(stream.name)
    try:
        index.save(temporary)
        temporary.replace(args.out)
    finally:
        temporary.unlink(missing_ok=True)
    print(f"Indexed {len(chunks)} chunks -> {args.out}")


if __name__ == "__main__":
    main()
