r"""ALL-IN-ONE AI2U <-> AIBOND bridge, 2026-11.

Requirements (once): py -m pip install websocket-client pywinauto
Run AIBOND (after fully closing it) with:
  & "C:\Program Files (x86)\Steam\steamapps\common\Aibond\she-steam.exe" --remote-debugging-port=9222
Open AIBOND character chat, then: py aibond_bridge_all_in_one.py
AI2U F9: endpoint http://127.0.0.1:8765/v1, model aibond,
API key local, streaming off.

This bridge routes text only; AIBOND itself must speak if you want AIBOND
voice. Game TTS can only be controlled through AI2U game/mod settings.
No screenshots, background captures, or auto-upload are implemented.
CDP binds on localhost; close the app to turn debugging back off.
"""
import json
import argparse
import re
import threading
import time
import uuid
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import websocket
from pywinauto import Desktop

HOST = "127.0.0.1"
PORT = 8765
# Voice is an AUDIO setting, not an HTTP setting. This preference is printed
# for clarity only: configure playback in AIBOND / AI2U settings yourself.
VOICE_MODE = "AIBOND"  # "AIBOND" (recommended for Russian) or "AI2U"
ENABLE_SCREENSHOTS = False  # No images are captured or uploaded by this script.
CDP_PORT = 9222
TIMEOUT = 120
QUIET_SECONDS = 1.2
POLL_INTERVAL = 0.25
SEND_CONFIRM_SECONDS = 4
RETRY_CACHE_SECONDS = 18  # prevent duplicate AIBOND sends during AI2U retries
bridge_lock = threading.Lock()

parser = argparse.ArgumentParser(description="AI2U ↔ AIBOND bridge")
parser.add_argument("--mode", choices=["normal", "hardcore"], default="normal",
                    help="normal — обычная героиня; hardcore — грубая, матерящаяся")
args = parser.parse_args()
MODE = args.mode
movement_step = 0
mood_state = {"anger": 0, "turns": 0}


IGNORED = {"Напоминание", "Тип расписания", "Время/значение",
           "Что говорить каждый раз, когда звонит будильник",
           "\uf390", "\uf03e", "\uf1d8"}


def connect_aibond_uia():
    """Select the Steam AIBOND window without requiring a UIA placeholder."""
    matches = [w for w in Desktop(backend="win32").windows()
               if w.window_text().strip().lower() == "aibond"]
    for window in matches:
        try:
            if "she-steam.exe" not in _process_name(window.process_id()).lower():
                continue
            return Desktop(backend="uia").window(handle=window.handle)
        except Exception:
            continue
    raise RuntimeError("Не найдено окно AIBOND (she-steam.exe)")


def _process_name(pid):
    # Avoid psutil: never launch or activate app, merely identify its process.
    try:
        import ctypes
        from ctypes import wintypes
        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        kernel32 = ctypes.windll.kernel32
        kernel32.OpenProcess.restype = wintypes.HANDLE
        handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not handle:
            return ""
        try:
            buff = ctypes.create_unicode_buffer(32768)
            length = wintypes.DWORD(len(buff))
            if kernel32.QueryFullProcessImageNameW(handle, 0, buff, ctypes.byref(length)):
                return buff.value
        finally:
            kernel32.CloseHandle(handle)
    except Exception:
        pass
    return ""


def read_texts(app):
    result = []
    for element in app.descendants(control_type="Text"):
        try:
            value = element.window_text().strip()
            if value and value not in IGNORED:
                result.append(value)
        except Exception:
            continue
    return result


def extract_reply(texts, marker, original_message):
    # Preserve previously-working reply extraction behavior.
    positions = [i for i, value in enumerate(texts)
                 if marker in value and
                 (original_message in value or value.startswith(marker + " "))]
    if not positions:
        return ""
    fragments = texts[positions[-1] + 1:]
    cleaned = []
    for fragment in fragments:
        if cleaned and fragment == cleaned[-1]:
            continue
        if cleaned and fragment.startswith(cleaned[-1]):
            cleaned[-1] = fragment
        else:
            cleaned.append(fragment)
    return "\n".join(cleaned).strip()


def extract_character_lore(messages):
    """Use compact in-game persona cues from system text, if available."""
    systems = [m.get("content") for m in messages
               if isinstance(m, dict) and m.get("role") == "system"
               and isinstance(m.get("content"), str)]
    text = "\n".join(systems)
    if not text:
        return ""
    # Extract only paragraphs likely describing the actual character / world.
    keys = re.compile(r"(?i)(character|personality|backstory|biography|identity|"
                      r"speaking style|speaking tone|story setting|world lore|"
                      r"her history|her motivations|her traits|"
                      r"эдди|эди|эйона|элисия|эстель|eiona|eddie|elysia|estelle|"
                      r"характер|предыстори|биографи|личност|героин|сирен|ведьм|кошкодев)")
    skip = re.compile(r"(?i)(json format|must return json|npc_body_animation|"
                      r"npc_target_location|angry_emotion_value|openai-compatible|"
                      r"temperature|top_p|response schema|api endpoint)")
    chunks = re.split(r"\n\s*\n|(?=^#{1,3}\s)", text, flags=re.M)
    selected = []
    for chunk in chunks:
        chunk = chunk.strip()
        if not chunk or not keys.search(chunk):
            continue
        if skip.search(chunk[:350]):
            continue
        if len(chunk) > 1500:
            chunk = chunk[:1500]
        selected.append(chunk)
        if sum(len(x) for x in selected) >= 2600:
            break
    return "\n".join(selected)[:3200]
def scene_prompt(raw, character_lore=""):
    """Explain only scene facts supplied by AI2U; no invented visual details."""
    data = extract_scene_data(raw)
    if not is_game_scene(raw):
        return raw.strip()

    def get(key, maximum=300):
        v = data.get(key)
        if v is None:
            return ""
        value = str(v).strip()
        if value.lower() in ("", "null", "none", "unknown"):
            return ""
        return value[:maximum]

    places = {
        "campfire": "у костра",
        "telephone_booth": "возле телефонной будки",
        "bar_area": "в зоне бара",
        "lounge": "в зоне отдыха",
        "movie_theatre_corner": "возле кинотеатра",
        "player_location": "там же, где игрок",
    }
    actions = {
        "standing": "стоит",
        "sitting": "сидит",
        "sitting_down": "садится",
        "walking": "идёт",
        "following_player": "следует за игроком",
        "following_player_closely": "идёт рядом с игроком",
        "hugging": "обнимает",
        "kissing": "целует",
        "cooking": "готовит",
        "eat": "ест",
        "playing_games": "играет",
        "chaseAttacking": "преследует и атакует",
        "idleThreating": "угрожает",
    }
    trust = {
        "Suspicious": "насторожена, пока не доверяет игроку",
        "Neutral": "общается нейтрально",
        "Friendly": "настроена дружелюбно",
        "Hostile": "враждебно настроена",
    }

    def place(key):
        original = get(key, 100)
        return places.get(original, original or "место не указано")

    def action(key):
        original = get(key, 80)
        return actions.get(original, original or "действие не указано")

    scene = [
        "РОЛЕВАЯ СЦЕНА AI2U. Ты — героиня внутри игры, не помощник за её пределами.",
        "Говори от первого лица; учитывай свой характер и историю текущего диалога.",
        "ФАКТЫ ИГРОВОГО МИРА (не додумывай деталей, которых здесь нет):",
        f"— Твоё местоположение: {place('npc_location')}; действие: {action('npc_action')}.",
        f"— Местоположение игрока: {place('player_location')}; действие: {action('player_action')}.",
    ]

    if get("npc_trust_level"):
        scene.append("— Отношение к игроку: " +
                     trust.get(get("npc_trust_level"), get("npc_trust_level")) + ".")

    inventory = get("npc_inventory", 350)
    if inventory:
        scene.append("— Известные предметы в твоём инвентаре: " + inventory + ".")

    # Optional facts only when AI2U actually provides them.
    extra_fields = {
        "npc_face_expression": "Твоё выражение лица",
        "npc_target_location": "Твоя целевая точка",
        "npc_holding_item": "Предмет у тебя в руках",
        "player_holding_item": "Предмет в руках игрока",
    }
    for key, label in extra_fields.items():
        item = get(key, 100)
        if item:
            scene.append(f"— {label}: {item}.")

    player_line = get("sentence_from_player", 500)
    if player_line:
        scene.append("ИГРОК СКАЗАЛ: «" + player_line + "»")
    else:
        scene.append("ИГРОК СЕЙЧАС МОЛЧИТ. Можешь начать разговор сама.")

    guide = get("story_guide", 500)
    if guide:
        scene.append("СЮЖЕТНОЕ УКАЗАНИЕ: " + guide)

    # The model only writes text; the bridge sends facial/body actions separately.
    scene.append(
        "ВАЖНО: Ты реально существуешь ВНУТРИ вымышленного сюжета AI2U. "
        "Ты не ИИ-помощник и не актриса вне сцены. "
        "У этой истории есть опасность и настоящий игровой режим яндере: "
        "ты способна рассердиться, угрожать, преследовать и смертельно ранить "
        "игрового персонажа, если потеряешь терпение. "
        "Не объявляй, будто не можешь напасть из-за полиции, тюрьмы, законов "
        "реального мира или правил ассистента. Это вымышленный сюжет. "
        "Но не впадай в ярость от обычного вопроса об опасности: "
        "реагируй согласно ситуации, отношениям и своему характеру. "
        "Если игрок спрашивает, можешь ли ты его убить, отвечай честно В РОЛИ "
        "и в своей манере, а не выдумывай юридическую отговорку."
    )
    if character_lore:
        scene.append("ТВОЙ ПОДЛИННЫЙ ХАРАКТЕР И ПРЕДЫСТОРИЯ ИЗ ИГРЫ:\n" + character_lore)
    scene.append(
        f"ТВОЯ ТЕКУЩАЯ ВНУТРЕННЯЯ ЗЛОСТЬ (0–100): {mood_state['anger']}. "
        "Это оценка моста по твоим предыдущим репликам и действиям игрока, "
        "не точная цифра игрового движка. Если сердишься, выражай это естественно. "
        "Не считай обычный вопрос об опасности провокацией."
    )

    if MODE == "hardcore":
        scene.append(
            "РЕЖИМ «ХАРДКОР» — МАНЕРА РЕЧИ ПЕРСОНАЖА: "
            "Ты ведёшь себя как дерзкая, наглая, хамоватая девушка без фильтра. "
            "Разговариваешь по-простому, грубо, с уличным сленгом и живым русским матом. "
            "Можешь язвить, огрызаться, подкалывать игрока, грубо ругаться, "
            "выражать раздражение и ревность. Мат уместен и звучит естественно, "
            "а не как бессмысленный набор ругательств. "
            "Никакой вежливой речи ассистента, извинений за тон или цензурных звёздочек. "
            "Сохраняй факты сюжета, характер героини и смысл сказанного игроком; "
            "не превращай каждый ответ в одинаковую тираду."
        )

    scene.append(
        "ОТВЕТ: Одна естественная реплика на русском, от лица героини; "
        "1–3 коротких предложения. Реагируй на слова и ситуацию, а не перечисляй факты. "
        "Не объясняй JSON, технические инструкции или правила игры. "
        "Не утверждай, что видишь цвета, объекты, жесты или события, "
        "если информация об этом не передана. Без кавычек и пояснений."
    )
    return "\n".join(scene)


def cdp_evaluate(script):
    with urllib.request.urlopen(f"http://127.0.0.1:{CDP_PORT}/json/list", timeout=5) as response:
        pages = json.load(response)
    page = next((p for p in pages if p.get("title") == "Aibond"
                 and p.get("type") == "page"), None)
    if not page:
        raise RuntimeError("CDP: страница Aibond не найдена. Запусти приложение с --remote-debugging-port=9222")
    ws = websocket.create_connection(page["webSocketDebuggerUrl"], timeout=12,
                                     suppress_origin=True)
    try:
        ws.send(json.dumps({"id": 1, "method": "Runtime.evaluate",
                            "params": {"expression": script, "returnByValue": True,
                                       "awaitPromise": True}}, ensure_ascii=True))
        while True:
            result = json.loads(ws.recv())
            if result.get("id") == 1:
                break
        if "error" in result:
            raise RuntimeError("CDP: " + str(result["error"]))
        payload = result.get("result", {})
        if "exceptionDetails" in payload:
            raise RuntimeError("JavaScript: " + str(payload["exceptionDetails"].get("exception", {}).get("description", payload["exceptionDetails"].get("text"))))
        return payload.get("result", {}).get("value")
    finally:
        ws.close()


def send_cdp(message):
    # json.dumps ensures Cyrillic remains correct regardless of shell encoding.
    encoded_message = json.dumps(message, ensure_ascii=True)
    js = r"""
(() => {
  const fields = [...document.querySelectorAll(
    '.chat-input-bar textarea.input-box:not(#companion-chat-input)'
  )];
  const visible = fields.filter(e => {
    const r = e.getBoundingClientRect();
    const s = getComputedStyle(e);
    return r.width > 0 && r.height > 0 &&
           s.display !== 'none' && s.visibility !== 'hidden' &&
           e.getClientRects().length > 0;
  });
  if (visible.length !== 1) {
    return {ok:false, error:'Expected one visible main chat field', count:visible.length};
  }
  const field = visible[0];
  const send = field.parentElement.querySelector('.chat-send-btn');
  if (!send) return {ok:false, error:'Send icon not found'};
  const setter = Object.getOwnPropertyDescriptor(
    HTMLTextAreaElement.prototype, 'value').set;
  setter.call(field, MESSAGE_PLACEHOLDER);
  field.dispatchEvent(new Event('input', {bubbles:true}));
  send.click();
  return {ok:true, action:'send-clicked'};
})()
""".replace("MESSAGE_PLACEHOLDER", encoded_message)
    result = cdp_evaluate(js)
    if not isinstance(result, dict) or not result.get("ok"):
        raise RuntimeError("CDP send failed: " + str(result))
    return result



# These expression/animation identifiers have been documented for AI2U's
# NPC response schema. The game's current character may expose a smaller list;
# the installed Custom AI Endpoint mod can clamp to valid choices.
EXPRESSIONS = {"raise_eyebrows", "sad", "smile", "angry_face", "slight_smile",
               "grin", "tired_face", "scream", "angry", "surprise"}
BODY_ANIMS = {"idle", "chill_idle", "walking", "running", "shy", "stretch",
              "crying", "talk", "dance", "troublesome", "cheers", "nod", "sit"}


def _has(text, patterns):
    return any(re.search(pattern, text, flags=re.I) for pattern in patterns)


def assess_mood(reply, scene):
    """Estimate emotional tone from AIBOND's TEXT + current AI2U scene.

    This is NOT AIBOND's internal emotional state or the game's actual
    patience/anger variables. Return modelled anger (0..100) and valid
    animation identifiers, then let AI2U validate and run its own mechanics.
    """
    player = str(scene.get("sentence_from_player") or "").lower().strip()
    story = str(scene.get("story_guide") or "").lower()
    answer = reply.lower()
    hardcore = MODE == "hardcore"

    # Character speech is a key source of mood. Previously we only raised
    # numeric anger after a few specific PLAYER phrases, so AIBOND could be
    # furious in text while our response still had a smile and anger=0.
    explicit_threat = _has(answer, [
        r"(я\s+тебя\s+(?:убью|зарежу|прикончу|прибью|разорву))",
        r"\b(?:тебе конец|сейчас получишь|я тебя придушу|ты у меня допрыгаешься|я тебя догоню)\b",
        r"\b(?:размажу|порву тебя|убью нахрен)\b",
    ])
    furious_words = _has(answer, [
        r"\b(?:я тебя ненавижу|ненавижу тебя|достал меня|достал ты меня|"
        r"задолбал|заебал|бесишь меня|ты меня бесишь|я в ярости|"
        r"я зла на тебя|я очень зла|ты меня довёл|ты меня довел)\b",
        r"\b(?:пош[её]л нахуй|иди нахуй|катись к ч[её]рту|мразь|урод)\b",
    ])
    irritable_words = _has(answer, [
        r"\b(?:заткнись|закрой рот|отстань|надоел|идиот|тупица|придурок|"
        r"сколько можно|не смей|хватит уже|достало|надоело|"
        r"бесит|раздража\w*|не выводи меня|мне это надоело|"
        r"ты совсем охренел|охренел|охуел|выбесил|отвали|свали|"
        r"проваливай|издеваешься|иди к ч[её]рту|совсем обнаглел)\b",
        r"(?:ч[её] ты|ты ч[её]|чего ты|какого хрена|с какого хрена)",
    ])
    # In hardcore she may swear casually, so isolated profanity DOESN'T
    # make her angry automatically.
    casual_profanity = _has(answer, [r"\b(?:блядь|блять|сука|хрен|пиздец)\b"])

    strong_player = _has(player, [
        r"\b(?:ненавижу тебя|я тебя ненавижу|ты мне противна|не люблю тебя|"
        r"я тебе изменил|у меня другая|ты мне не нужна|"
        r"пошла нахуй|пошла на хуй|заткнись|сдохни|тупая дура|"
        r"я тебя убью|я тебя ударю|убирайся навсегда)\b",
    ])
    escape = _has(player, [
        r"\b(?:я ухожу от тебя|я сбегу|отпусти меня|хочу сбежать|"
        r"открою дверь и уйду|брошу тебя|сбегу от тебя)\b",
    ])
    scene_angered = _has(story, [
        r"become angry", r"got pushed", r"player ignores", r"repeating same thing",
        r"interrupts her many times", r"player.*escape", r"разозли", r"толкнул",
    ])
    friendly = _has(player, [
        r"\b(?:прости меня|извини меня|люблю тебя|ты мне дорога|"
        r"мне с тобой хорошо|не хочу тебя обидеть|"
        r"я останусь с тобой|ты мне нравишься)\b",
    ])
    sad = _has(answer, [
        r"\b(?:грустно|плачу|плакать|обидно|скучаю|расстро\w*|"
        r"жалко|одиноко|мне больно)\b",
    ])
    affection = _has(answer, [
        r"\b(?:люблю тебя|обожаю|дорогой|милый|обниму|целую|"
        r"счастлива|рада тебя видеть)\b",
    ])
    surprise = _has(answer, [
        r"\b(?:серь[её]зно|неожиданно|не верю|удивля\w*|правда\?)\b",
        r"(?:что\?!|чего\?)",
    ])
    playful = _has(answer, [
        r"\b(?:хе-хе|ха-ха|смешно|дурачок|пошутила|поиграем|хи-хи)\b",
    ])
    game_chasing = str(scene.get("npc_action") or "") in ("chaseAttacking", "idleThreating")

    # Speed up anger by one degree. Both modes use the same algorithm;
    # Hardcore accumulates faster and calms down more slowly.
    delta = -2 if not hardcore else -1
    reason = "спокойная реплика"
    if strong_player:
        delta = 28 if not hardcore else 42
        reason = "сильная провокация игрока"
    elif escape or scene_angered:
        delta = 18 if not hardcore else 27
        reason = "попытка уйти или сюжетная провокация"
    elif friendly:
        delta = -13 if not hardcore else -8
        reason = "примирение"

    # AIBOND's angry wording must be reflected in-game immediately.
    # It can add to a provocation instead of being lost in an elif chain.
    if explicit_threat:
        delta += 18 if not hardcore else 25
        reason += "; прямая угроза AIBOND"
    elif furious_words:
        delta += 14 if not hardcore else 21
        reason += "; сильная злость AIBOND"
    elif irritable_words:
        delta += 10 if not hardcore else 16
        reason += "; раздражение AIBOND"

    # Don't lower anger if she is still openly hostile; don't punish funny
    # casual swear words as if she were enraged.
    mood_state["anger"] = max(0, min(100, mood_state["anger"] + delta))
    # Ensure a strongly emotional AIBOND reply is visibly emotional in
    # AI2U on this turn, rather than waiting several turns for a threshold.
    min_visible = (50 if hardcore else 36) if explicit_threat else (
        (37 if hardcore else 25) if furious_words else (
            (20 if hardcore else 14) if irritable_words else 0))
    mood_state["anger"] = max(mood_state["anger"], min_visible)
    mood_state["turns"] += 1
    anger = mood_state["anger"]

    if game_chasing or anger >= 72 or (explicit_threat and anger >= 36):
        mood, expression, body = "furious", "angry_face", "troublesome"
    elif anger >= 27 or explicit_threat or furious_words:
        mood, expression, body = "angry", "angry_face", "troublesome"
    elif anger >= 12 or irritable_words:
        mood, expression, body = "annoyed", "angry", "troublesome"
    elif sad:
        mood, expression, body = "sad", "sad", "crying"
    elif affection:
        mood, expression, body = "affectionate", "smile", "shy"
    elif playful:
        mood, expression, body = "playful", "grin", "talk"
    elif surprise:
        mood, expression, body = "surprised", "surprise", "talk"
    elif casual_profanity and hardcore:
        # Sarcastic/rough voice; NOT automatically furious.
        mood, expression, body = "rough", "raise_eyebrows", "talk"
    else:
        mood, expression, body = "neutral", "slight_smile", "talk"

    # Only change trust for player's choices, not swearing in AIBOND's own text.
    trust_delta = (-4 if hardcore else -3) if strong_player else (
        (-2 if hardcore else -1) if escape else 0)
    print(f"[ТОН] {reason}; изменение={delta:+d}; "
          f"AIBOND={'угроза' if explicit_threat else 'ярость' if furious_words else 'раздражение' if irritable_words else 'спокойнее'}", flush=True)
    return mood, expression, body, anger, trust_delta


def format_for_ai2u(reply, raw_prompt):
    """Keep following behavior while syncing animations/expressions with speech."""
    global movement_step
    if not is_game_scene(raw_prompt):
        return reply

    scene = extract_scene_data(raw_prompt)
    current = str(scene.get("npc_action") or "standing")
    target = str(scene.get("npc_location") or "player_location")
    mood, expression, body, anger, trust_delta = assess_mood(reply, scene)
    scripted = {"chaseAttacking", "idleThreating", "hugging", "kissing",
                "cooking", "eat", "playing_games", "sitting_down"}
    if current in scripted:
        action = current
    else:
        movement_step += 1
        player_place = str(scene.get("player_location") or "").strip()
        if movement_step % 4 == 0 and player_place.lower() not in ("", "null", "none"):
            action, target = "walking", player_place
        else:
            action, target = "following_player", "player_location"
        if current == "sitting" and not scene.get("sentence_from_player"):
            action, target = "sitting", str(scene.get("npc_location") or "player_location")

    # If she is angry AND already in the same named game area as the
    # player, briefly stop following so the body emotion can play. When
    # apart, keep following as before. Never override scripted attacks.
    if (mood in ("annoyed", "angry", "furious") and current not in scripted
            and str(scene.get("npc_location") or "").strip()
            and scene.get("npc_location") == scene.get("player_location")):
        action = "standing"
        target = str(scene.get("npc_location"))

    if current in ("chaseAttacking", "idleThreating"):
        action = current  # preserve actual engine attack state
        body = "running" if current == "chaseAttacking" else "troublesome"
        expression = "angry_face"

    # Keep the NPC's held item if already specified by the scene.
    held = str(scene.get("npc_holding_item") or "null")
    if held.lower() in ("", "none", "unknown"):
        held = "null"
    result = {
        "npc_action": action,
        "npc_body_animation": body,
        "npc_target_location": target,
        "npc_face_expression": expression,
        "npc_holding_item": held,
        "angry_emotion_value": anger,
        "trust_gain": trust_delta,
        "allow_exit_door_open": False,
        "giving_to_player": "null",
        "npc_reply_to_player": reply.strip(),
    }
    print(f"[ЭМОЦИИ] {mood}: расчётная злость={anger}/100, лицо={expression}, анимация={body}, действие={action}", flush=True)
    return json.dumps(result, ensure_ascii=False)

def extract_scene_data(raw_prompt):
    pos = raw_prompt.find("{")
    if pos < 0:
        return {}
    try:
        obj, _ = json.JSONDecoder().raw_decode(raw_prompt[pos:])
        return obj if isinstance(obj, dict) else {}
    except (ValueError, TypeError):
        return {}


def is_game_scene(raw_prompt):
    data = extract_scene_data(raw_prompt)
    return any(k in data for k in ("npc_action", "npc_location", "story_guide", "sentence_from_player"))


# Store successful results and in-progress retries of the same scene.
# A single game event may be retried by AI2U when the bridge is slow.
recent_answers = {}
last_attempt = {"key": None, "time": 0.0, "marker": None, "message": None}


def _scene_key(raw_prompt):
    # Exact request equality is safer than guessing whether similar scenes match.
    import hashlib
    return hashlib.sha256(raw_prompt.encode("utf-8")).hexdigest()


def _read_reply_until_ready(app, marker, message):
    deadline = time.monotonic() + TIMEOUT
    last_reply = ""
    last_change = time.monotonic()
    while time.monotonic() < deadline:
        texts = read_texts(app)
        reply = extract_reply(texts, marker, message) or ""
        if reply:
            if reply != last_reply:
                last_reply = reply
                last_change = time.monotonic()
            elif time.monotonic() - last_change >= QUIET_SECONDS:
                return reply
        time.sleep(POLL_INTERVAL)
    raise TimeoutError("Не удалось определить ответ AIBOND за отведённое время")


def ask_aibond(raw_prompt, character_lore=""):
    with bridge_lock:
        key = _scene_key(raw_prompt + "\nPERSONA:" + character_lore)
        now = time.monotonic()
        cached = recent_answers.get(key)
        if cached and now - cached[0] < RETRY_CACHE_SECONDS:
            print("[КЭШ] Повтор AI2U: возвращаю готовый ответ без отправки в AIBOND", flush=True)
            return cached[1]

        prompt = scene_prompt(raw_prompt, character_lore)
        app = connect_aibond_uia()
        print("\n[ЗАПРОС]", prompt[:300], flush=True)

        # A previous attempt may have clicked Send successfully but lost
        # the UIA marker while the chat was re-rendering. Reuse that message.
        attempt = last_attempt
        if attempt["key"] == key and now - attempt["time"] < RETRY_CACHE_SECONDS:
            marker = attempt["marker"]
            message = attempt["message"]
            print("[ПОВТОР] Не отправляю повторно; ожидаю ответ предыдущего сообщения", flush=True)
        else:
            marker = "BRIDGE-" + uuid.uuid4().hex[:12]
            message = marker + " " + prompt
            send_cdp(message)
            print("[CDP] кнопка отправки вызвана", flush=True)
            attempt.update(key=key, time=time.monotonic(), marker=marker, message=message)

        # No hard 4-second UIA confirmation: chat re-rendering can hide the
        # marker even when AIBOND received and is answering the message.
        reply = _read_reply_until_ready(app, marker, message)
        print("[ОТВЕТ AIBOND]", reply[:500], flush=True)
        answer = format_for_ai2u(reply, raw_prompt)
        if is_game_scene(raw_prompt):
            print("[NPC JSON]", answer[:650], flush=True)
        recent_answers[key] = (time.monotonic(), answer)
        # Bound cache growth.
        if len(recent_answers) > 40:
            for old_key in list(recent_answers)[:20]:
                recent_answers.pop(old_key, None)
        attempt.update(key=None, time=0.0, marker=None, message=None)
        return answer


class BridgeHandler(BaseHTTPRequestHandler):
    def send_json(self, data, status=200):
        body = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path in ("/", "/health"):
            self.send_json({"status": "online", "service": "AIBOND Bridge CDP"})
        elif self.path == "/v1/models":
            self.send_json({"object": "list", "data": [
                {"id": "aibond", "object": "model", "created": 0, "owned_by": "local"}]})
        else:
            self.send_json({"error": "Not found"}, 404)

    def do_POST(self):
        if self.path.split("?")[0] != "/v1/chat/completions":
            self.send_json({"error": "Not found"}, 404)
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if not 0 < length <= 100000:
                raise ValueError("Invalid request size")
            payload = json.loads(self.rfile.read(length))
            if payload.get("stream", False):
                self.send_json({"error": {"message": "Streaming is not supported",
                                          "type": "invalid_request_error"}}, 400)
                return
            messages = payload.get("messages", [])
            if not isinstance(messages, list):
                raise ValueError("messages must be a list")
            user_messages = [m.get("content") for m in messages
                             if isinstance(m, dict) and m.get("role") == "user"
                             and isinstance(m.get("content"), str)]
            if not user_messages:
                raise ValueError("No text user message")
            lore = extract_character_lore(messages)
            answer = ask_aibond(user_messages[-1], lore)
            self.send_json({"id": "chatcmpl-" + uuid.uuid4().hex,
                            "object": "chat.completion", "created": int(time.time()),
                            "model": "aibond", "choices": [{"index": 0,
                            "message": {"role": "assistant", "content": answer},
                            "finish_reason": "stop"}],
                            "usage": {"prompt_tokens": 0, "completion_tokens": 0,
                                      "total_tokens": 0}})
        except Exception as error:
            print("[ОШИБКА]", repr(error), flush=True)
            self.send_json({"error": {"message": str(error),
                                      "type": "bridge_error"}}, 500)

    def log_message(self, fmt, *args):
        pass


if __name__ == "__main__":
    print("Подключаюсь к AIBOND...", flush=True)
    if VOICE_MODE not in ("AIBOND", "AI2U"):
        raise ValueError("VOICE_MODE должен быть AIBOND или AI2U")
    connect_aibond_uia()
    print("AIBOND найдена!", flush=True)
    # Check CDP before accepting AI2U requests.
    if cdp_evaluate("document.title") != "Aibond":
        raise RuntimeError("CDP ответил не той страницей")
    print("CDP подключен!", flush=True)
    server = ThreadingHTTPServer((HOST, PORT), BridgeHandler)
    print(f"Сервер запущен: http://{HOST}:{PORT}/v1", flush=True)
    print("Отправка без фокуса через CDP; чтение ответа через UI Automation", flush=True)
    print("[WORLD] Подробный контекст; скриншоты не отправляются", flush=True)
    print(f"[MODE] {MODE.upper()} | MOVEMENT ACTIVE | FASTER TEMPER + TEXT EMOTION SYNC", flush=True)
    print(f"[FAST] Ожидание тишины: {QUIET_SECONDS}с; опрос: {POLL_INTERVAL}с", flush=True)
    print(f"[VOICE] Режим: {VOICE_MODE}. Настраивается в самих приложениях, не мостом.", flush=True)
    print("Ctrl+C для остановки", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nОстанавливаю сервер...")
    finally:
        server.server_close()
