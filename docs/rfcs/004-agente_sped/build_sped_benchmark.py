from __future__ import annotations

import argparse
import hashlib
import random
from pathlib import Path


def mutate_line(line: str, seq: int) -> str:
    parts = line.split("|")
    if len(parts) < 3:
        return line

    code = parts[1]
    if code == "C100":
        if len(parts) > 8:
            parts[8] = str(int(parts[8]) + seq) if parts[8].isdigit() else f"{parts[8]}{seq}"
        if len(parts) > 9:
            parts[9] = f"{parts[9][:34]}{seq % 10}" if parts[9] else f"CHV{seq}"
    elif code == "K230":
        if len(parts) > 4:
            parts[4] = f"{parts[4]}_{seq}"

    return "|".join(parts)


def build_benchmark(
    input_path: Path,
    output_path: Path,
    target_size_mb: int,
    seed: int,
    *,
    overwrite: bool = False,
) -> dict[str, int | str]:
    if target_size_mb < 1:
        raise ValueError("target_size_mb deve ser maior ou igual a 1.")

    if input_path.resolve() == output_path.resolve():
        raise ValueError("O arquivo de saida deve ser diferente da fixture de entrada.")

    if output_path.exists() and not overwrite:
        raise FileExistsError(f"Arquivo de saida ja existe: {output_path}. Use --overwrite para substituir.")

    rng = random.Random(seed)
    src = input_path.read_text(encoding="utf-8").splitlines()
    if not src:
        raise ValueError("Arquivo de entrada vazio.")

    target_bytes = target_size_mb * 1024 * 1024
    output_path.parent.mkdir(parents=True, exist_ok=True)

    sha256 = hashlib.sha256()
    current_bytes = 0
    total_lines = 0
    seq = rng.randint(0, 10_000)

    with output_path.open("w", encoding="utf-8", newline="\n") as handle:
        while current_bytes < target_bytes:
            for line in src:
                mutated = mutate_line(line, seq)
                seq += 1
                text = mutated + "\n"
                handle.write(text)
                encoded = text.encode("utf-8")
                sha256.update(encoded)
                current_bytes += len(encoded)
                total_lines += 1
                if current_bytes >= target_bytes:
                    break

    size_bytes = output_path.stat().st_size
    return {
        "target_bytes": target_bytes,
        "size_bytes": size_bytes,
        "lines": total_lines,
        "seed": seed,
        "sha256": sha256.hexdigest(),
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Gera massa sintetica deterministica para benchmark de staging.")
    parser.add_argument(
        "--input",
        default="rfcs/agente-sped-fiscal/research/data/sped_icms_analysis_reference.txt",
        help="Arquivo SPED base.",
    )
    parser.add_argument("--output", default="sped_icms_benchmark_100mb.txt", help="Arquivo de saida.")
    parser.add_argument("--target-size-mb", type=int, default=100, help="Tamanho alvo em MB.")
    parser.add_argument("--seed", type=int, default=42, help="Seed deterministico.")
    parser.add_argument("--overwrite", action="store_true", help="Sobrescreve o arquivo de saida se ele existir.")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    stats = build_benchmark(
        input_path=Path(args.input).resolve(),
        output_path=Path(args.output).resolve(),
        target_size_mb=args.target_size_mb,
        seed=args.seed,
        overwrite=args.overwrite,
    )

    print("=== BUILD SPED BENCHMARK ===")
    print("aviso: massa tecnica para benchmark; nao valida para PVA.")
    print(f"input: {Path(args.input).resolve()}")
    print(f"output: {Path(args.output).resolve()}")
    print(f"target_bytes: {stats['target_bytes']}")
    print(f"size_bytes: {stats['size_bytes']}")
    print(f"lines: {stats['lines']}")
    print(f"seed: {stats['seed']}")
    print(f"sha256: {stats['sha256']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
