def expected_word(seed: int) -> int:
    return 4096 * (3 if seed & 1 else 1)
