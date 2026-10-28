import pytest

from edge_backup.verify import Manifest, VerificationError, check_file_count, check_rows


@pytest.mark.parametrize(
    ("rows", "named"), [(None, "no number"), (7, "7")], ids=["no row count", "another row count"]
)
def test_a_restored_table_that_differs_from_the_manifest_says_what_the_manifest_names(rows, named):
    manifest = Manifest(app="radio", audio_files=None, rows=rows, files=None)

    with pytest.raises(VerificationError, match=f"^3 rows restored, the manifest names {named}$"):
        check_rows(manifest, 3)


def test_a_restored_volume_that_differs_from_a_manifest_without_a_count_says_so():
    message = r"^2 files restored, the manifest names no number$"
    with pytest.raises(VerificationError, match=message):
        check_file_count(None, 2, "files")
