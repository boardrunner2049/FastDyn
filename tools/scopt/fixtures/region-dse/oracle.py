def expected_word(seed: int) -> int:
    return 131072 * (5 if seed & 1 else 7)
