"""Independent fixed-work oracle for the Cortex-M3 fixture."""

ITERATIONS = 65536
INCREMENT = 0x9E3779B9


def expected_word(seed: int) -> int:
    word = seed
    for _ in range(ITERATIONS):
        reflected = int(f"{word:032b}"[::-1], 2)
        word = (reflected + INCREMENT) % (1 << 32)
    return word
