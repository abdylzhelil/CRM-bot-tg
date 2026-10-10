from app.services.antispam import AuthorInfo, Behavior, score_message, text_hash


def live() -> AuthorInfo:
    return AuthorInfo(user_id=123456, username="ivan", first_name="Иван", last_name="Петров")


def test_live_author_low_score():
    assert score_message(live(), "Ищу разработчика для лендинга").value < 30


def test_spam_text_and_account():
    a = AuthorInfo(user_id=8_000_000_000, first_name="Cash$$ t.me/x", has_photo=False)
    s = score_message(a, "Заработок без вложений! Пиши в лс https://t.me/x")
    assert s.value >= 60


def test_cross_group_duplicate():
    s = score_message(live(), "ищу сайт", Behavior(same_text_other_groups=2))
    assert s.value >= 40


def test_hash_ignores_case_and_punctuation():
    assert text_hash("Ищу сайт!") == text_hash("ищу   сайт")
