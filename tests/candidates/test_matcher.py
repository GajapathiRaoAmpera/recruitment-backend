from app.candidates.matcher import match_candidate

A = {
    "id": "a",
    "student_id": "21CS1001",
    "full_name": "Asha Rao",
    "email": "asha@x.com",
    "phone": "9876543210",
}
B = {
    "id": "b",
    "student_id": "21CS1002",
    "full_name": "Asha Rao",
    "email": "b@x.com",
    "phone": "9876543210",
}
C = {
    "id": "c",
    "student_id": "21CS1003",
    "full_name": "Ravi Kumar",
    "email": "ravi@x.com",
    "phone": None,
}


def test_email_wins_over_ambiguous_name_and_phone():
    assert match_candidate("cv.pdf", "Asha Rao | asha@x.com | 98765 43210", [A, B, C]) == "a"


def test_phone_with_formatting():
    assert match_candidate("cv.pdf", "Ravi call me +91-98765-43210", [B, C]) == "b"


def test_student_id_in_text():
    assert match_candidate("cv.pdf", "Roll No: 21cs1003, B.Tech", [A, C]) == "c"


def test_shared_name_is_not_guessed():
    assert match_candidate("cv.pdf", "Asha Rao, no contact details", [A, B]) is None


def test_name_unique_ok_and_no_signal_none():
    assert match_candidate("cv.pdf", "Ravi Kumar, B.Tech", [A, C]) == "c"
    assert match_candidate("cv.pdf", "Somebody Else", [A, B, C]) is None
    assert match_candidate("cv.pdf", None, [A, B, C]) is None


def test_filename_by_id_still_works():
    assert match_candidate("21CS1001.pdf", None, [A, C]) == "a"
