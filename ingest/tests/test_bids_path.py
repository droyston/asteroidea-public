"""`bids_path.entity_key`."""

from asteroidea_ingest.bids_path import entity_key

RUN1 = "sub-p62cs/ses-1/func/sub-p62cs_ses-1_task-movie_run-1"


def test_an_events_file_and_its_bold_share_the_key() -> None:
    assert entity_key(f"{RUN1}_events.tsv") == RUN1
    assert entity_key(f"{RUN1}_bold.nii.gz") == RUN1


def test_a_different_run_session_or_directory_does_not_match() -> None:
    events = entity_key(f"{RUN1}_events.tsv")
    run2 = RUN1.replace("run-1", "run-2")
    assert entity_key(f"{run2}_bold.nii.gz") != events
    ses2 = "sub-p62cs/ses-2/func/sub-p62cs_ses-2_task-movie_run-1"
    assert entity_key(f"{ses2}_bold.nii.gz") != events
    beh = "sub-p62cs/ses-1/beh/sub-p62cs_ses-1_task-movie_run-1"
    assert entity_key(f"{beh}_bold.nii.gz") != events


def test_a_dataset_level_events_file_keeps_its_own_key() -> None:
    assert entity_key("task-movie_events.tsv") == "task-movie"


def test_a_file_with_no_entities_returns_its_stem() -> None:
    assert entity_key("participants.tsv") == "participants"
    assert entity_key("sub-01/anat/scan.nii.gz") == "sub-01/anat/scan"
