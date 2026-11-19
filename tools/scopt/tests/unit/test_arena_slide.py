from scopt.passes.arena_slide import best_page_shift


def test_page_slide_preserves_order_and_scores_taken_edge():
    edges = [(4050, 4120, 4150, 80), (0, 32, 64, 2)]
    assert best_page_shift(edges, 4096, 4095, lambda: None) == (64, 80, 0)
    assert best_page_shift(edges, 4096, 63, lambda: None) == (0, 80, 80)


def test_page_slide_preserves_the_original_on_a_tie():
    assert best_page_shift([(64, 96, 128, 10)], 4096, 65536, lambda: None) == (0, 0, 0)
