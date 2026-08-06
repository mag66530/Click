"""
reviews.py – отзывы Яндекс.Бизнеса: разбор, отбор, промпт, очередь.

Здесь только логика без браузера и без сети: всё, что можно проверить
тестом на сохранённой странице (`tests_reviews_fixture.json`). Работа с
живой страницей – в `yb_playwright.py`, вызов Gemini – в `llm.py`.

Главное, что выяснилось при разборе живой карточки: раздел отзывов
отдаёт весь список готовым JSON прямо в HTML – `window.__PRELOAD_DATA`,
путь `initialState.edit.reviews.list`. Вёрстку разбирать не надо, а
«отзыв без ответа» определяется однозначно: у него нет `owner_comment`.
Это заметно надёжнее, чем искать пустое поле глазами по классам, –
редизайн Яндекса такую проверку не ломает.

Правила отбора – решения заказчика, не наши догадки:
  • черновик пишем только на отзывы в 5 звёзд. Всё, что ниже, уходит
    человеку со ссылкой: цена ошибки в ответе на недовольство выше,
    чем польза от автоматизации;
  • отзыв без текста (одни звёзды) пропускаем молча;
  • возраст отзыва не важен – отвечаем и на старые;
  • имя подставляем, только если оно похоже на человеческое имя,
    иначе «Клиент».
"""

from __future__ import annotations

import json
import re
from difflib import SequenceMatcher
from datetime import datetime, timezone
from pathlib import Path

import paths
import projects_data as pdata

# Метка сборки – одна на всё приложение, лежит в build.py. Держать её
# в каждом модуле своей строкой уже ломалось: у одного файла осталось
# старое значение, и Click принялся перезагружать все модули на каждое
# нажатие, пока не выбило по памяти.
from build import BUILD  # noqa: F401

# ─── Статусы элемента очереди ───────────────────────────────────────
DRAFTED = "drafted"                  # черновик готов, ждём человека
NEEDS_HUMAN = "needs-human"          # меньше 5 звёзд – машина не отвечает
NO_DRAFT = "no-draft"                # LLM не смог, поле для ручного ввода
ANSWERED = "answered"                # ответ опубликован
SKIPPED = "skipped"                  # человек пропустил
ALREADY = "already-answered"         # ответ появился, пока мы ждали
FAILED = "failed"                    # публикация не прошла

OPEN_STATUSES = (DRAFTED, NEEDS_HUMAN, NO_DRAFT, FAILED)

GOOD_RATING = 5                      # ниже – человеку (решение заказчика)

# Потолок черновиков на город. Ставили 5 – исходили из того, что неотвеченных
# отзывов на карточке единицы. Первый боевой прогон это опроверг: в Ереване их
# было 12, в Баку 6, и восемь отзывов остались без черновика не потому, что с
# ними что-то не так, а просто упёрлись в потолок. Подняли до размера страницы:
# от лишних запросов к Gemini теперь защищает пауза между вызовами (llm.py),
# а не искусственный обрез.
MAX_DRAFTS_PER_CITY = 20
FALLBACK_NAME = "Клиент"


# ════════════════════════════════════════════════════════════════════
#  Разбор данных страницы
# ════════════════════════════════════════════════════════════════════

def normalize(raw: dict) -> dict:
    """
    Отзыв Яндекса – к плоскому виду, с которым дальше работает всё Click.

    Со страницы отзывы теперь приходят уже плоскими: тянуть в Python весь
    блок состояния было расточительно по памяти. Но фикстура и старые
    данные лежат в исходном виде, поэтому понимаем оба.
    """
    if "answered" in raw and "text" in raw:
        return raw                                   # уже плоский
    author = raw.get("author")
    return {
        "id": raw.get("id"),
        "author": (author.get("user") if isinstance(author, dict) else author) or "",
        "rating": int(raw.get("rating") or 0),
        "text": (raw.get("full_text") or raw.get("snippet") or "").strip(),
        "time_created": raw.get("time_created") or 0,
        "answered": bool(((raw.get("owner_comment") or {}).get("text") or "").strip()),
    }


def extract_list(preload: dict | None) -> dict:
    """
    Вытащить блок отзывов из `window.__PRELOAD_DATA`.

    Возвращает {'items': [...], 'total': N, 'shown': N}. Пустой блок –
    это не ошибка: у карточки может не быть ни одного отзыва.
    """
    node = (((preload or {}).get("initialState") or {}).get("edit") or {}).get("reviews") or {}
    lst = node.get("list") or {}
    items = [normalize(x) for x in (lst.get("items") or [])]
    pager = lst.get("pager") or {}
    return {
        "items": items,
        "shown": len(items),
        # total – сколько отзывов у карточки всего; за первую страницу не ходим
        "total": int(pager.get("total") or len(items)),
        "limit": int(pager.get("limit") or len(items)),
    }


def is_unanswered(item: dict) -> bool:
    """Есть ли ответ компании. Понимаем и плоский вид, и исходный."""
    if "answered" in item:
        return not item["answered"]
    return not ((item.get("owner_comment") or {}).get("text") or "").strip()


def review_text(item: dict) -> str:
    return (item.get("text") or item.get("full_text") or item.get("snippet") or "").strip()


def review_author(item: dict) -> str:
    a = item.get("author")
    if isinstance(a, dict):
        a = a.get("user")
    return (a or "").strip()


# ════════════════════════════════════════════════════════════════════
#  Имя автора
# ════════════════════════════════════════════════════════════════════

# Имя = одно-два слова из букв. Дефисы и апострофы внутри слова разрешены
# (Анна-Мария, О'Коннор), одиночная буква с точкой отбрасывается как
# инициал: «Надежда Х.» → «Надежда».
_WORD = re.compile(r"^[А-Яа-яЁёA-Za-z][А-Яа-яЁёA-Za-z'\-]{1,}$")

# Ругань и грубость – без списка мата в исходниках: ловим по корням,
# этого хватает, а спорные случаи всё равно увидит человек.
_RUDE = re.compile(r"(ху[йяеё]|пизд|бля|еба|ёба|сук[аи]|мраз|говн|xyi|fuck|shit|bitch)", re.I)


def nice_name(raw: str | None) -> str | None:
    """
    Имя для обращения или None, если подставлять его не стоит.

    Берём, если это одно-два нормальных слова без цифр, ссылок и брани.
    Инициал («Х.») отбрасываем. Всё остальное – никнеймы, «Пользователь
    123», ссылки, грубость – не берём: пусть лучше будет «Клиент», чем
    странное обращение от имени бренда.

    Возвращаем ТОЛЬКО имя, без фамилии. Яндекс показывает автора как
    «Павел Филиппов», и первый прогон дал «Уважаемый Павел Филиппов!» –
    по-русски это звучит казённо. Во всех примерах заказчика обращение
    по имени: «Уважаемая Анастасия!».
    """
    s = (raw or "").strip()
    if not s or len(s) > 40:
        return None
    if any(ch.isdigit() for ch in s) or "@" in s or "http" in s.lower():
        return None
    if _RUDE.search(s):
        return None

    words = [w for w in re.split(r"\s+", s) if w]
    # Отбрасываем инициалы вида «Х.» – в обращении они не нужны.
    words = [w for w in words if not re.fullmatch(r"[А-ЯЁA-Z]\.?", w)]
    if not words or len(words) > 2:
        return None
    if not all(_WORD.fullmatch(w.rstrip(".")) for w in words):
        return None

    name = words[0].rstrip(".")
    # Имя латиницей – это транслит («Michail», «Felix», «Vakhit»). В русском
    # ответе оно смотрится чужеродно, а пол по нему не прочитать. Заказчик
    # решила так: не понимаем имя – пишем «клиент».
    if re.search(r"[A-Za-z]", name):
        return None
    return name


def name_for_prompt(raw: str | None) -> str:
    return nice_name(raw) or FALLBACK_NAME


# ════════════════════════════════════════════════════════════════════
#  Промпт
# ════════════════════════════════════════════════════════════════════

def default_prompt(project_id: str) -> str:
    """Заводской промпт из кода – текст заказчика, как он есть в документе."""
    return pdata.REVIEW_PROMPTS.get(project_id, "")


def project_prompt(project_id: str) -> str:
    """
    Промпт проекта: заводской из кода, поверх – правка из приложения.
    Хранится там же, где окончания постов (ветка click-data), иначе в
    облаке правка исчезла бы при первом перезапуске контейнера.
    """
    try:
        import repo_store
        saved = repo_store.load(f"reviews-prompt-{project_id}")
        if saved and (saved.get("prompt") or "").strip():
            return saved["prompt"]
    except Exception:  # noqa: BLE001 – нет сети/токена: берём заводской
        pass
    return default_prompt(project_id)


def save_project_prompt(project_id: str, text: str) -> str:
    import repo_store
    return repo_store.save(f"reviews-prompt-{project_id}", {"prompt": text or ""},
                           f"Click: промпт ответов на отзывы {project_id}")


def is_custom_prompt(project_id: str) -> bool:
    return project_prompt(project_id).strip() != default_prompt(project_id).strip()


def build_prompt(prompt_template: str, item: dict, project_id: str = "") -> str:
    """
    Подставить отзыв и имя в промпт проекта и добавить общие правила.

    Правила (тире, обращение по полу, без разметки) идут последними и почти
    одинаковы для всех пяти брендов. Держим их отдельно, чтобы правка промпта
    в «Настройках» их не отменяла. Различается один пункт – про ассортиментную
    фразу: у МПИ она внутри абзаца, у остальных отдельным абзацем.
    """
    text = review_text(item)
    name = name_for_prompt(review_author(item))
    body = (prompt_template
            .replace(pdata.REVIEW_TEXT_MARK, text)
            .replace(pdata.REVIEW_NAME_MARK, name))
    return body.rstrip() + "\n" + pdata.review_common_rules(project_id)


ASSORTMENT_NEAR = 0.75      # насколько абзац похож на эталон, чтобы счесть его тем самым


def fix_assortment(project_id: str, text: str) -> str:
    """
    Вернуть ассортиментной фразе эталонный вид.

    Фраза у каждого бренда своя и вставляется ДОСЛОВНО – требование заказчика.
    Модель соблюдает это не всегда: в боевом прогоне один ответ ушёл в Яндекс
    со строкой «трубы, armatura – собираем и отгружаем без задержек», слово
    внезапно оказалось латиницей. В трёх десятках ответов такое глазами не
    ловится.

    Сплошной запрет латиницы тут не годится: в металлоторговле законно
    встречаются AISI 304, DIN, A2. Зато сама фраза известна заранее – ищем
    похожий на неё абзац и заменяем эталоном. Не нашли похожего – не трогаем
    ничего: это уже другая беда, её ловит looks_broken.
    """
    want = (pdata.REVIEW_ASSORTMENT.get(project_id) or "").strip()
    if not want or not text:
        return text
    parts = text.split("\n\n")
    for i, part in enumerate(parts):
        p = part.strip()
        if not p or p == want:
            continue
        if SequenceMatcher(None, p.lower(), want.lower()).ratio() >= ASSORTMENT_NEAR:
            parts[i] = want
            break
    return "\n\n".join(parts)


# Подпись и зачин у МПИ ищем регулярками: модель ставит их то отдельной
# строкой, то в общий поток, а иногда чуть меняет знак в конце.
_MPI_SIGN_RX = re.compile(r"\s*С\s+уважением\b.*$", re.S | re.I)
_MPI_OPENER_RX = re.compile(r"^\s*Спасибо[^.!?\n]*[.!]?\s*", re.I)
_SENTENCE_SPLIT_RX = re.compile(r"(?<=[.!?])\s+")


def fix_mpi_shape(text: str) -> str:
    """
    Привести ответ МПИ к виду, который заказчик утвердила образцами:

        Спасибо за отзыв и рекомендацию!
        <пустая строка>
        Благодарим вас за отзыв! …про этот отзыв… Обращайтесь к нам ещё, всегда
        готовы предложить широкий ассортимент металлопроката и индивидуальный
        подход к каждому клиенту.
        <пустая строка>
        С уважением, МетПромИнтекс.

    Правим только ФОРМУ – первую строку, последнее предложение и подпись.
    Середину не трогаем: что там написать, знает модель, а не мы.

    Зачем код, если про это сказано в промпте: в боевом прогоне модель
    нарушила вид во всех трёх ответах подряд – меняла первую строку, теряла
    фразу про ассортимент, склеивала всё в один абзац без пустых строк.
    Промпт делает это редким, код – невозможным.
    """
    t = (text or "").strip()
    if not t:
        return t

    t = _MPI_SIGN_RX.sub("", t).strip()          # подпись поставим сами
    t = _MPI_OPENER_RX.sub("", t, count=1).strip()   # и первую строку тоже
    body = re.sub(r"\s*\n\s*", " ", t).strip()   # середина – ровно один абзац
    body = re.sub(r"[ \t]{2,}", " ", body)

    tail = pdata.REVIEW_MPI_TAIL
    sentences = [s for s in _SENTENCE_SPLIT_RX.split(body) if s.strip()]
    # Фраза про ассортимент могла прийти в чуть другом виде – тогда меняем её
    # на эталонную, а не дописываем вторую такую же.
    for i, s in enumerate(sentences):
        if SequenceMatcher(None, s.lower(), tail.lower()).ratio() >= ASSORTMENT_NEAR:
            sentences.pop(i)
            break
    # Прощальная фраза вместо ассортиментной («Будем рады видеть вас снова!») –
    # это ровно та ошибка, на которую жаловалась заказчик.
    if sentences and _MPI_FAREWELL_RX.search(sentences[-1]):
        sentences.pop()
    body = " ".join(s.strip() for s in sentences if s.strip()).strip()
    body = (body + " " + tail).strip() if body else tail

    return f"{pdata.REVIEW_MPI_OPENER}\n\n{body}\n\n{pdata.REVIEW_MPI_SIGN}"


# Прощания, которыми модель подменяла фразу про ассортимент.
_MPI_FAREWELL_RX = re.compile(
    r"(будем рады|ждём вас|ждем вас|рады видеть|до новых встреч|обращайтесь)", re.I)


def clean_draft(text: str, project_id: str = "") -> str:
    """
    Причесать ответ после модели.

    Правила вроде «не используй длинное тире» модель выполняет через раз,
    а замена – работа на одну строку и не ошибается. Заодно убираем
    markdown-звёздочки и кавычки, в которые модель иногда заворачивает
    весь ответ целиком, и возвращаем эталонный вид ассортиментной фразе.
    """
    t = (text or "").strip()
    if len(t) > 1 and t[0] in "\"«" and t[-1] in "\"»":
        t = t[1:-1].strip()
    t = t.replace("\u2014", "\u2013")          # длинное тире – на короткое
    t = re.sub(r"\*\*(.+?)\*\*", r"\1", t)      # **жирный** markdown
    t = re.sub(r"(?m)^#{1,6}\s*", "", t)        # заголовки решётками
    t = re.sub(r"\n{3,}", "\n\n", t).strip()    # лишние пустые строки
    if project_id == "MPI":
        return fix_mpi_shape(t)                 # у МПИ вид ответа свой, см. функцию
    return fix_assortment(project_id, t) if project_id else t


def looks_cut_off(text: str) -> bool:
    """
    Похоже ли, что ответ оборван на середине фразы.

    Нужно из-за первого боевого прогона: модель тратила лимит на размышления
    и обрывала текст, а человек мог, не приглядываясь, отправить под именем
    бренда половину предложения. Причину мы починили, но проверка дешёвая и
    остаётся страховкой.
    """
    t = (text or "").strip()
    return bool(t) and not t.endswith((".", "!", "?", "…", ")", "»", ":"))


# Следы «мыслей вслух» модели: она рассуждает по-английски о структуре
# ответа, и куски этих рассуждений попадали прямо в черновик – например
# «Salutation a sentence? If P1 is ... MUST be 2 sentences».
_THINKING_TRACE = re.compile(
    r"(sentence|paragraph|prompt says|line\s*\d|let'?s\s|must be|is proper|"
    r"\bP\d\b|check if|structure)", re.I)


def looks_broken(text: str) -> bool:
    """
    Черновик неудачный: пустой, оборванный или с рассуждениями модели вместо
    ответа. Такие надо переписать, а не отправлять.

    Одного признака «оборван» мало: один черновик заканчивался точкой и
    поэтому считался целым, хотя целиком состоял из размышлений –
    «...Prompt says: «Обращение: Уважаемый(ая) Эрик Айрапетян!»».
    """
    t = (text or "").strip()
    if not t:
        return True
    if looks_cut_off(t):
        return True
    if _THINKING_TRACE.search(t):
        return True
    # Модель копировала «Уважаемый(ая)» прямо из промпта – в ответе под
    # именем бренда так писать нельзя, пол выбирается один.
    if "(ая)" in t or "(a)" in t.lower():
        return True
    # Ответ на русском, а тут сплошная латиница – значит, это не ответ.
    letters = [c for c in t if c.isalpha()]
    if letters:
        latin = sum(1 for c in letters if "a" <= c.lower() <= "z")
        if latin / len(letters) > 0.25:
            return True
    return False


def banned_words(text: str) -> list[str]:
    """
    Запрещённые промптами слова, которые всё-таки просочились в ответ.
    Не блокируем публикацию – просто показываем человеку предупреждение.
    """
    low = (text or "").lower()
    return [w for w in pdata.REVIEW_BANNED_WORDS if re.search(rf"\b{re.escape(w)}", low)]


# ════════════════════════════════════════════════════════════════════
#  Отбор отзывов на одном городе
# ════════════════════════════════════════════════════════════════════

def triage(items: list[dict], max_drafts: int = MAX_DRAFTS_PER_CITY) -> dict:
    """
    Разложить отзывы города по корзинам. Никаких сетевых вызовов –
    только решение, что с чем делать.

      to_draft    – на них зовём Gemini
      needs_human – меньше 5 звёзд, машина не отвечает
      no_text     – одни звёзды, пропускаем молча
      over_limit  – не влезли в потолок черновиков на город
    """
    unanswered = [it for it in items if is_unanswered(it)]
    to_draft, needs_human, no_text = [], [], []

    for it in unanswered:
        if not review_text(it):
            no_text.append(it)
        elif int(it.get("rating") or 0) < GOOD_RATING:
            needs_human.append(it)
        else:
            to_draft.append(it)

    over_limit = to_draft[max_drafts:]
    return {
        "unanswered": unanswered,
        "to_draft": to_draft[:max_drafts],
        "needs_human": needs_human,
        "no_text": no_text,
        "over_limit": over_limit,
    }


def as_queue_item(item: dict, *, project_id: str, city: str, company_url: str,
                  reviews_url: str, status: str, draft: str = "", note: str = "") -> dict:
    """Плоская запись для очереди – ровно то, что нужно UI и отчёту."""
    created = item.get("time_created")
    return {
        "reviewId": item.get("id"),
        "projectId": project_id,
        "city": city,
        "companyUrl": company_url,
        "reviewsUrl": reviews_url,
        "author": review_author(item),
        "rating": int(item.get("rating") or 0),
        "text": review_text(item),
        "createdAt": _ms_to_iso(created),
        "status": status,
        "draft": draft,
        "finalText": "",
        "note": note,
        "collectedAt": datetime.now(timezone.utc).isoformat(),
    }


def _ms_to_iso(ms) -> str:
    try:
        return datetime.fromtimestamp(int(ms) / 1000, tz=timezone.utc).isoformat()
    except Exception:  # noqa: BLE001 – формат времени у Яндекса менялся, ронять из-за него нечего
        return ""


# ════════════════════════════════════════════════════════════════════
#  Очередь на подтверждение
# ════════════════════════════════════════════════════════════════════
#
# Во время прогона пишем в локальный файл – он быстрый. Наружу (в
# repo_store) выталкиваем в конце прогона и после действий человека:
# в облаке файловая система временная, и без этого собранная работа
# на 140 городов пропадёт при первом же перезапуске контейнера.

# Как и в runner.py – модульная переменная, а не вызов на каждом обращении:
# тесты подменяют её на временную папку, чтобы не писать в живые данные.
USERS_DATA = paths.data_root()


def queue_path(project_id: str) -> Path:
    d = USERS_DATA / project_id
    d.mkdir(parents=True, exist_ok=True)
    return d / "reviews-queue.json"


def _store_key(project_id: str) -> str:
    return f"reviews-queue-{project_id}"


def load_queue(project_id: str) -> list[dict]:
    fp = queue_path(project_id)
    if fp.exists():
        try:
            data = json.loads(fp.read_text(encoding="utf-8"))
            if isinstance(data, dict) and isinstance(data.get("items"), list):
                return data["items"]
        except (json.JSONDecodeError, OSError):
            pass
    # Локально пусто – в облаке это норма после перезапуска, тянем из хранилища.
    try:
        import repo_store
        saved = repo_store.load(_store_key(project_id))
        if saved and isinstance(saved.get("items"), list):
            _write_local(project_id, saved["items"])
            return saved["items"]
    except Exception:  # noqa: BLE001 – нет сети/токена: работаем с пустой очередью
        pass
    return []


def _write_local(project_id: str, items: list[dict]) -> None:
    fp = queue_path(project_id)
    tmp = fp.with_suffix(".tmp")
    tmp.write_text(json.dumps({"items": items}, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(fp)


def save_queue(project_id: str, items: list[dict], push: bool = False) -> str:
    """
    Сохранить очередь. push=True – ещё и наружу, чтобы пережила
    перезапуск. Возвращает описание «куда сохранили» либо причину, по
    которой наружу не получилось (не роняя прогон из-за этого).
    """
    _write_local(project_id, items)
    if not push:
        return "сохранено локально"
    try:
        import repo_store
        return repo_store.save(_store_key(project_id), {"items": items},
                               f"Click: очередь ответов на отзывы {project_id}")
    except Exception as e:  # noqa: BLE001
        return f"наружу сохранить не вышло: {e}"


def merge(existing: list[dict], fresh: list[dict]) -> list[dict]:
    """
    Долить свежесобранное, не трогая то, что человек уже разобрал.
    Ключ – reviewId: повторный прогон по тому же городу не плодит дубли
    и не затирает уже написанный вручную ответ.
    """
    by_id = {it.get("reviewId"): it for it in existing if it.get("reviewId")}
    out = list(existing)
    for it in fresh:
        rid = it.get("reviewId")
        if not rid or rid in by_id:
            continue
        by_id[rid] = it
        out.append(it)
    return out


def open_items(items: list[dict]) -> list[dict]:
    return [it for it in items if it.get("status") in OPEN_STATUSES]


def counters(items: list[dict]) -> dict:
    """Сводка для отчёта и для плашек в интерфейсе."""
    out = {"found": 0, "drafted": 0, "needsHuman": 0, "answered": 0,
           "skipped": 0, "noDraft": 0, "failed": 0}
    for it in items:
        out["found"] += 1
        st = it.get("status")
        out[{DRAFTED: "drafted", NEEDS_HUMAN: "needsHuman", ANSWERED: "answered",
             SKIPPED: "skipped", NO_DRAFT: "noDraft", ALREADY: "answered",
             FAILED: "failed"}.get(st, "failed")] += 1
    return out
