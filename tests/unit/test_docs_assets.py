"""Included reference links must work locally and in the accepting repository."""
from fastdyn.docs_assets import repair_reference_links


def test_reference_links_follow_book_pages_and_accepting_repository():
    chapter = {'source_path': 'general/writing-virtuals.md', 'content':
               '[Virtuals](VirtualsAndModifiers.md#modifiers)\n'
               '[Other reference](VirtualPreprocessing.md)\n'
               '[Config](../configs/bare_bones.toml)\n', 'sub_items': []}
    book = {'items': [{'Chapter': chapter}]}
    context = {'config': {'output': {'html': {
        'git-repository-url': 'https://github.com/PSecLab/FastDyn',
        'edit-url-template': 'https://github.com/PSecLab/FastDyn/edit/main/docs/book/{path}'}}}}
    repair_reference_links(book, context)
    assert '(virtuals.md#modifiers)' in chapter['content']
    assert '(https://github.com/PSecLab/FastDyn/blob/main/docs/VirtualPreprocessing.md)' in chapter['content']
    assert '(https://github.com/PSecLab/FastDyn/blob/main/configs/bare_bones.toml)' in chapter['content']


def test_fresh_nix_assets_make_the_destination_root_writable(tmp_path):
    from fastdyn.docs_assets import stage

    root = tmp_path / "docs"
    root.mkdir()
    (root / "assets.toml").write_text("# pinned assets\n")
    prebuilt = tmp_path / "nix-assets"
    prebuilt.mkdir()
    asset = prebuilt / "editor.js"
    asset.write_text("// editor\n")
    asset.chmod(0o444)
    prebuilt.chmod(0o555)
    try:
        stage(root, "book", prebuilt)
        destination = root / "book/vendor"
        assert (destination / ".asset-version").is_file()
        assert (destination / "editor.js").read_text() == "// editor\n"
        assert destination.stat().st_mode & 0o200
        (root / "assets.toml").write_text("# updated pins\n")
        stage(root, "book", prebuilt)
    finally:
        prebuilt.chmod(0o755)
