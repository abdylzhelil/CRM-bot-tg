"""Скоринг автора и текста: 0 (живой) … 100 (спам). Без ИИ — только эвристики."""
import hashlib
import re
from dataclasses import dataclass, field

# Грубая оценка «новый аккаунт» по ID: выше порога — аккаунт заведён недавно.
NEW_ACCOUNT_ID = 7_000_000_000

_LINK = re.compile(r"(https?://|t\.me/|www\.|\w+\.(ru|com|org|me|xyz)\b)", re.I)
_PM = re.compile(r"(пиш\w+\s+в\s+(лс|личк\w+|директ)|в\s+лс\b|\+?\d[\d\s\-()]{9,})", re.I)
_SPAM_WORDS = re.compile(
    r"(заработ\w+|крипт\w+|инвестиц\w+|бесплатн\w+|казино|ставк\w+|пассивн\w+ доход|"
    r"быстр\w+ деньг\w+|без вложени\w+|раскрутк\w+ бесплатно)",
    re.I,
)
# «серые» темы: запрещёнка, обнал, документы без оснований, интим, лёгкие деньги — это не клиенты
_GREY = re.compile(
    r"(закладк\w*|наркот\w*|мефедрон\w*|амфетамин\w*|кокаин\w*|гашиш\w*|травк\w* в наличии|"
    r"обнал\w*|обналич\w*|дроп(ы|ов|ам)?\b|аренд\w* карт|карт\w* в аренду|пробив\w*|даркнет\w*|"
    r"купить (права|диплом|справк\w*|аттестат\w*)|без экзамен\w*|поддельн\w*|фальшив\w*|"
    r"интим\w*|эскорт\w*|вебкам\w*|18\+|для взрослых|"
    r"букмекер\w*|ставк\w* на спорт|договорн\w* матч\w*|"
    r"(доход|заработ\w*|зп|оплата)\s*от\s*\d[\d\s]*\s*(₽|р\.?|руб\w*|\$|к)?\s*(в|за)\s*(день|сутки|недел\w*))",
    re.I,
)
_EMOJI = re.compile("[\U0001F300-\U0001FAFF☀-➿]")


@dataclass
class AuthorInfo:
    user_id: int
    username: str = ""
    first_name: str = ""
    last_name: str = ""
    bot: bool = False
    scam: bool = False
    fake: bool = False
    deleted: bool = False
    has_photo: bool = True


@dataclass
class Behavior:
    same_text_other_groups: int = 0  # тот же текст в других группах
    msgs_last_10min: int = 0


@dataclass
class Score:
    value: int = 0
    reasons: list[str] = field(default_factory=list)

    def add(self, points: int, reason: str) -> None:
        self.value = min(100, self.value + points)
        self.reasons.append(reason)


def text_hash(text: str) -> str:
    norm = re.sub(r"\W+", " ", text.lower()).strip()
    return hashlib.sha1(norm.encode()).hexdigest()


def _weird_name(name: str) -> bool:
    if not name:
        return False
    letters = sum(c.isalpha() for c in name)
    if letters < len(name) * 0.6:
        return True
    return bool(_LINK.search(name)) or "@" in name


def score_message(author: AuthorInfo, text: str, behavior: Behavior | None = None) -> Score:
    s = Score()
    b = behavior or Behavior()
    if author.bot:
        s.add(60, "бот")
    if author.scam or author.fake:
        s.add(60, "scam/fake")
    if author.deleted:
        s.add(50, "удалённый аккаунт")
    if not author.has_photo:
        s.add(10, "нет фото")
    if not author.username:
        s.add(10, "нет username")
    if _weird_name(f"{author.first_name} {author.last_name}".strip()):
        s.add(20, "странное имя")
    if author.user_id > NEW_ACCOUNT_ID:
        s.add(10, "новый аккаунт")

    if _LINK.search(text):
        s.add(15, "ссылка")
    if _PM.search(text):
        s.add(10, "«пиши в ЛС»/телефон")
    if _SPAM_WORDS.search(text):
        s.add(25, "спам-слова")
    if _GREY.search(text):
        s.add(45, "серая тема")
    if len(_EMOJI.findall(text)) > 5:
        s.add(10, "много эмодзи")

    if b.same_text_other_groups >= 1:
        s.add(40, f"тот же текст в {b.same_text_other_groups + 1} группах")
    if b.msgs_last_10min >= 5:
        s.add(20, "высокая частота")
    return s
