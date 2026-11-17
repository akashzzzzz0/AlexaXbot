"""Alexa Own AI: low-memory multilingual conversational engine.

This is an offline intent/context engine, not a trained generative LLM.
It uses only the Python standard library and keeps the latest five turns
per conversation in SQLite. It never calls third-party AI providers.
"""
import ast
import datetime as dt
import hmac
import json
import math
import os
import re
import sqlite3
import sys
import threading
from zoneinfo import ZoneInfo
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

# Resolve the service directory explicitly so the dialogue module is found even
# when the platform launches this file with a different working directory.
APP_DIR = os.path.dirname(os.path.abspath(__file__))
if APP_DIR not in sys.path:
    sys.path.insert(0, APP_DIR)

try:
    from .conversation_brain import respond as conversation_respond
except ImportError:  # support direct execution: python app.py
    from conversation_brain import respond as conversation_respond

try:
    from .model_runtime import generate as local_model_generate, status as model_runtime_status
except ImportError:
    from model_runtime import generate as local_model_generate, status as model_runtime_status

PORT = int(os.getenv("PORT", "3000"))
API_TOKEN = os.getenv("ALEXA_AI_TOKEN", "")
DB_PATH = os.getenv("ALEXA_AI_DB_PATH", "alexa_memory.sqlite3")
MAX_BODY = 32_000
MAX_HISTORY = 20
INDIA_TZ = ZoneInfo("Asia/Kolkata")
DB_LOCK = threading.RLock()

# Approximate transliteration for common Urdu and Hindi chat vocabulary.
URDU = {
"ا":"a","آ":"aa","ب":"b","پ":"p","ت":"t","ٹ":"t","ث":"s","ج":"j","چ":"ch","ح":"h","خ":"kh","د":"d","ڈ":"d","ذ":"z","ر":"r","ڑ":"r","ز":"z","ژ":"zh","س":"s","ش":"sh","ص":"s","ض":"z","ط":"t","ظ":"z","ع":"","غ":"gh","ف":"f","ق":"q","ک":"k","گ":"g","ل":"l","م":"m","ن":"n","ں":"n","و":"o","ؤ":"o","ہ":"h","ھ":"h","ۃ":"h","ء":"","ئ":"i","ی":"i","ي":"i","ك":"k","ى":"a","ے":"e","َ":"a","ِ":"i","ُ":"u","ّ":"","ً":"an","ٍ":"in","ٌ":"un","ٓ":""
}
DEV = {
"अ":"a","आ":"aa","इ":"i","ई":"ee","उ":"u","ऊ":"oo","ऋ":"ri","ए":"e","ऐ":"ai","ओ":"o","औ":"au",
"क":"k","ख":"kh","ग":"g","घ":"gh","ङ":"n","च":"ch","छ":"chh","ज":"j","झ":"jh","ञ":"n",
"ट":"t","ठ":"th","ड":"d","ढ":"dh","ण":"n","त":"t","थ":"th","द":"d","ध":"dh","न":"n",
"प":"p","फ":"ph","ब":"b","भ":"bh","म":"m","य":"y","र":"r","ल":"l","व":"v","श":"sh","ष":"sh","स":"s","ह":"h",
"क्ष":"ksh","त्र":"tr","ज्ञ":"gy","ड़":"r","ढ़":"rh","ँ":"n","ं":"n","ः":"h","़":"",
"ा":"a","ि":"i","ी":"ee","ु":"u","ू":"oo","ृ":"ri","े":"e","ै":"ai","ो":"o","ौ":"au","्":"","़":""
}
DEV_CONS = set("कखगघङचछजझञटठडढणतथदधनपफबभमयरलवशषसहड़ढ़")

def transliterate(text):
    out = []
    i = 0
    while i < len(text):
        pair = text[i:i+2]
        if pair in DEV:
            out.append(DEV[pair]); i += 2; continue
        ch = text[i]
        if ch in DEV:
            # Inherent 'a' after a consonant unless a vowel sign or virama follows.
            val = DEV[ch]
            if ch in DEV_CONS:
                nxt = text[i+1:i+2]
                if nxt not in ("्","ा","ि","ी","ु","ू","ृ","े","ै","ो","ौ"):
                    val += "a"
            out.append(val)
        elif ch in URDU:
            out.append(URDU[ch])
        else:
            out.append(ch)
        i += 1
    return "".join(out)

def normalize(value):
    value = transliterate(str(value or "").strip().lower())
    value = value.replace("’", "'").replace("‘", "'").replace("،", " ")
    value = re.sub(r"[^\w\s+*/().%\-]", " ", value, flags=re.UNICODE)
    return re.sub(r"\s+", " ", value).strip()

def script_of(value):
    s = str(value or "")
    if any("\u0900" <= c <= "\u097f" for c in s):
        return "hi"
    if any("\u0600" <= c <= "\u06ff" for c in s):
        return "ur"
    return "roman"

def detect_language(raw):
    script = script_of(raw)
    if script == "hi":
        return "hi"
    if script == "ur":
        return "ur"
    t = normalize(raw)
    # Only two reply modes: English, or Roman Hindi/Urdu.
    # Latin-script Hindi and Urdu overlap heavily, so treat them as one mode.
    if re.search(r"\b(the|is|you|your|what|why|how|hello|please|thanks|can|will|would|should|this|that|i|we|they|where|when|who|doing|good|morning|evening|today|tomorrow|because|about|with|from|for|my|do|did|have|has|had|am|was|were|it|to|of|on|in|at|and|or|but|not|just|tell|say|something|there|here)\b", t):
        return "en"
    return "ur"

REPLIES = {
"en": {
"hello":"Hey! How's it going? What's up?",
"how":"I'm doing well! How are you?",
"thanks":"You're welcome! Happy to help.",
"identity":"I'm Alexa, your friendly assistant. What can I help you with?",
"bored":"Let's make this interesting. Want a joke, a fun fact, or just a chat?",
"sad":"Sorry things feel rough. Want to tell me what's going on?",
"happy":"That's great news! What happened?",
"joke":"Why did the computer go to the doctor? It caught a virus. 😄",
"bye":"See you later! Take care.",
"yes":"Alright! Tell me a little more.",
"no":"No worries. What would you rather talk about?",
"doing":"Just here chatting with you. What are you up to?",
"where":"I am right here in this chat. What is up?",
"whatsup":"Not much, just chatting with you. How about you?",
"unknown":"I'm listening. Could you rephrase that or add a little context so I can understand exactly?"
},
"ur": {
"hello":"Assalam o alaikum! Kya haal hain? Aaj kya scene hai?",
"how":"Main theek hoon! Aap sunao, sab kaisa chal raha hai?",
"thanks":"Koi baat nahi! Khushi hui help karke.",
"identity":"Main Alexa hoon, aapki friendly assistant. Batao, kis cheez mein help chahiye?",
"bored":"Chalo kuch interesting baat karte hain! Joke sunna hai ya koi mazedar fact?",
"sad":"Oh, sun kar afsos hua. Agar batana chaho to kya hua? Main sun rahi hoon.",
"happy":"Wah, yeh to achi baat hai! Kya khushkhabri hai?",
"joke":"Teacher: Homework kahan hai? Student: Sir, cloud mein save kiya tha, badal aa gaye! 😄",
"bye":"Allah hafiz! Phir baat hoti hai. Apna khayal rakhna!",
"yes":"Achha ji! Thora aur batao, phir main behtar samajh sakungi.",
"no":"Theek hai, koi masla nahi. Phir kis bare mein baat karein?",
"doing":"Bas yahin tumse baat kar rahi hoon. Tum sunao?",
"where":"Yahin chat mein hoon. Batao, kya scene hai?",
"whatsup":"Bas tumse baat ho rahi hai. Tum sunao, kya haal hai?",
"unknown":"Haan, main sun rahi hoon. Thora aur context de do taake main sahi jawab de sakun."
},
"hi": {
"hello":"Arey hello! Kaise ho? Aaj kya chal raha hai?",
"how":"Main badhiya hoon! Tum batao, sab kaisa chal raha hai?",
"thanks":"Koi baat nahi! Help karke achha laga.",
"identity":"Main Alexa hoon, tumhari friendly assistant. Batao, kya help chahiye?",
"bored":"Chalo kuch mazedaar karte hain! Joke sunoge ya koi interesting fact?",
"sad":"Ye sunkar bura laga. Agar batana chaho to kya hua? Main sun rahi hoon.",
"happy":"Arey wah, ye to bahut achhi baat hai! Kya good news hai?",
"joke":"Teacher: Homework kahan hai? Student: Sir, Google Drive mein tha, internet chala gaya! 😄",
"bye":"Phir milte hain! Apna khayal rakhna.",
"yes":"Achha! Thoda aur batao, main samajhne ki koshish karti hoon.",
"no":"Koi baat nahi. Phir kis baare mein baat karein?",
"doing":"Bas tumse baat kar rahi hoon. Tum kya kar rahe ho?",
"where":"Yahin chat mein hoon. Tum kahan ho?",
"whatsup":"Bas yahin hoon! Tum batao, kya chal raha hai?",
"unknown":"Haan, main sun rahi hoon. Thoda aur context do taake main sahi jawab de sakun."
},
"pa": {
"hello":"Oye hoye! Ki haal aa? Sab changa?",
"how":"Main vadiya haan ji! Tussi sunao, ki haal chaal ne?",
"thanks":"Koi gall nahi veere! Help karke changa laggeya.",
"identity":"Main Alexa haan ji! Dassyo, tuhadi ki help karaan?",
"bored":"Oye bore kyun ho rahe o? Koi joke sunna ya gallan maariye?",
"sad":"Aah, eh sun ke changa nahi laggeya. Je dil kare taan dasso ki hoya, main sun rahi haan.",
"happy":"O balle balle! Eh taan vadiya khabar aa. Ki hoya ji?",
"joke":"Master ji: Homework kithe aa? Student: Sir, cloud te si, ajj badal chhaye hoye ne! 😄",
"bye":"Changa fer! Apna khayal rakhyo, fer gall karde aan.",
"yes":"Haanji, dasso fer! Hor thoda samjhao.",
"no":"Chalo koi gall nahi ji. Hor ki gall kariye?",
"doing":"Bas tuhade naal gallan kar rahi haan ji. Tussi dasso?",
"where":"Ethe chat vich hi haan ji. Tussi kithe o?",
"whatsup":"Bas gallan-baatan chal rahi aan. Tussi sunao, ki haal aa?",
"unknown":"Haanji, main sun rahi aan. Gall thodi hor samjha deo taake main sahi jawab de sakaan."
}
}

def india_now():
    """Return the current aware datetime in India Standard Time."""
    return dt.datetime.now(INDIA_TZ)


def safe_math(expression):
    expression = expression.replace("×", "*").replace("÷", "/").replace("^", "**")
    if not re.fullmatch(r"[0-9\s.+\-*/()%]*", expression):
        return None
    try:
        node = ast.parse(expression, mode="eval")
        def calc(n):
            if isinstance(n, ast.Expression): return calc(n.body)
            if isinstance(n, ast.Constant) and type(n.value) in (int, float): return n.value
            if isinstance(n, ast.UnaryOp) and isinstance(n.op, (ast.UAdd, ast.USub)):
                v = calc(n.operand); return v if isinstance(n.op, ast.UAdd) else -v
            if isinstance(n, ast.BinOp) and isinstance(n.op, (ast.Add, ast.Sub, ast.Mult, ast.Div, ast.FloorDiv, ast.Mod, ast.Pow)):
                a, b = calc(n.left), calc(n.right)
                if isinstance(n.op, ast.Add): v = a+b
                elif isinstance(n.op, ast.Sub): v = a-b
                elif isinstance(n.op, ast.Mult): v = a*b
                elif isinstance(n.op, ast.Div): v = a/b
                elif isinstance(n.op, ast.FloorDiv): v = a//b
                elif isinstance(n.op, ast.Mod): v = a%b
                else:
                    if abs(b) > 8: raise ValueError()
                    v = a**b
                if abs(v) > 10**15: raise ValueError()
                return v
            raise ValueError()
        result = calc(node)
        if not math.isfinite(float(result)): return None
        return int(result) if int(result) == result else round(result, 6)
    except (SyntaxError, ValueError, TypeError, ZeroDivisionError, OverflowError):
        return None

def memory_connection():
    conn = sqlite3.connect(DB_PATH, timeout=8)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("CREATE TABLE IF NOT EXISTS turns (conversation TEXT NOT NULL, seq INTEGER PRIMARY KEY AUTOINCREMENT, role TEXT NOT NULL, content TEXT NOT NULL, created_at TEXT NOT NULL)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_turns_conversation_seq ON turns(conversation, seq)")
    return conn

def get_history(conversation, supplied, current_message=""):
    supplied_messages = [x for x in supplied if isinstance(x, dict)]
    if supplied_messages and supplied_messages[-1].get("role") == "user" and str(supplied_messages[-1].get("content", "")) == str(current_message):
        supplied_messages = supplied_messages[:-1]
    if conversation:
        with DB_LOCK:
            with memory_connection() as conn:
                rows = conn.execute("SELECT role, content FROM turns WHERE conversation=? ORDER BY seq DESC LIMIT ?", (conversation, MAX_HISTORY)).fetchall()
        if rows:
            return [{"role": role, "content": content} for role, content in reversed(rows)]
    # The bot caller also has its own persistent five-message history. Use it
    # when the microservice database is new or was reset during a redeploy.
    return supplied_messages[-MAX_HISTORY:]

def save_turns(conversation, user_text, reply):
    if not conversation:
        return
    with DB_LOCK:
        with memory_connection() as conn:
            conn.execute("INSERT INTO turns(conversation, role, content, created_at) VALUES(?,?,?,?)", (conversation, "user", user_text[:4000], dt.datetime.now(dt.timezone.utc).isoformat()))
            conn.execute("INSERT INTO turns(conversation, role, content, created_at) VALUES(?,?,?,?)", (conversation, "assistant", reply[:4000], dt.datetime.now(dt.timezone.utc).isoformat()))
            conn.execute("DELETE FROM turns WHERE conversation=? AND seq NOT IN (SELECT seq FROM turns WHERE conversation=? ORDER BY seq DESC LIMIT ?)", (conversation, conversation, MAX_HISTORY))

def response_for(message, history):
    raw = str(message or "").strip()
    lang = detect_language(raw)
    t = normalize(raw)
    r = REPLIES[lang]

    # The installed bootstrap router (conversation_respond) handles local
    # dialogue matches and performs at most one hosted-model request. Calling
    # the model here first caused a second inference attempt after a timeout.
    # If no model is configured/available, retain the local fallback behavior.
    if not raw:
        return r["hello"]

    # Keep emoji-only input intact for contextual dialogue matching; normalize()
    # intentionally strips symbols for word-based intent rules.
    brain_reply = conversation_respond(raw, t or raw, lang, history)
    if brain_reply:
        return brain_reply

    # Deterministic intent and short-follow-up rules run before paid/remote inference.

    # Natural Roman Hindi/Urdu small talk and conversational repair.
    if re.search(r"\b(sab theek|sab thik|sab thek)\b", t) and re.search(r"\b(kahan thi|kha th+i+|kaha thi|kidhar thi|tum thi|tum ho)\b", t):
        return "Haan, sab theek chal raha hai. Main yahin thi, tum sunao? Kya chal raha hai?"
    if re.search(r"\b(kis|kaun se|kon se)\b", t) and re.search(r"\b(alfaz|lafz|ilfaz|words|tarah)\b", t) and re.search(r"\b(samj|samajh|samjhe|samjha)\w*\b", t):
        prior = " ".join(normalize(x.get("content", "")) for x in history[-MAX_HISTORY:] if isinstance(x, dict))
        if any(word in prior for word in ("rephrase", "doosre alfaaz", "apne words", "context", "samajh")):
            return "Arey, meri galti! Tumne bilkul theek kaha tha. Tumhari pehli baat samajh aa gayi thi—mujhe dobara poochna nahi chahiye tha."
        return "Mera matlab hai, jis tarah tum normally baat karte ho usi tarah batao. Main samajhne ki koshish karungi."
    if re.fullmatch(r"(are+ yaar|aray yaar|oye yaar|uff yaar|yaar)[!. ]*", t):
        return "Arey kya hua? Meri baat se confusion hua kya? Batao, main dhyan se samajhti hoon."
    # Common speech-to-text and casual Roman Urdu spellings for "speak / go ahead".
    if re.fullmatch(r"(?:bool|bol|bo+l+)\s*(?:ly+|le|na|do)?", t) or t in {"bolo", "bolo na", "bol na", "bolly", "bollyy", "suno", "sun na", "sunn"}:
        if lang == "en":
            return "I'm listening. Go ahead—what did you want to say?"
        if lang == "pa":
            return "Haanji, dasso! Main sun rahi aan."
        return "Haan ji, bolo! Main sun rahi hoon."
    # A greeting plus Alexa's name or a casual call should be treated as a direct greeting.
    if re.search(r"\balexa\b", t) and re.search(r"\b(oye|oy+|hello|hi|hey|baby)\b", t):
        if lang == "en":
            return "Hey! I'm here. What's up?"
        return "Oyyyy! Haan bolo, sun rahi hoon."
    if re.search(r"\b(suno|sun na|listen|listen up)\b", t):
        if lang == "en":
            return "I'm listening. What's up?"
        return "Haan ji, sun rahi hoon. Bolo?"
    if any(mark in raw for mark in ("السلام علیکم", "السلام عليكم", "السلامعلیکم", "سلام", "नमस्ते", "नमस्कार", "हैलो")):
        return r["hello"]
    if any(mark in raw for mark in ("آپ کیسے ہیں", "آپ کیسے ہو", "آپ کا حال")):
        return r["how"]
    math_text = re.sub(r"^(what is|calculate|solve|kitna hota hai|batao|btao|iska jawab)\s+", "", t)
    if re.search(r"\d\s*[+*/%\-]\s*\d", math_text) or re.search(r"\d\s*[×÷^]\s*\d", raw):
        value = safe_math(math_text if math_text != t else raw)
        if value is not None:
            if lang == "en": return "The answer is " + str(value) + "."
            if lang == "hi": return "Iska jawab " + str(value) + " hai."
            if lang == "pa": return "Isda jawab " + str(value) + " aa."
            return "Iska jawab " + str(value) + " hai."
    if re.search(r"\b(good morning|subah|subha|morning)\b", t):
        return "Good morning! Aaj ka din kaisa ja raha hai?" if lang != "en" else "Good morning! How's your day going?"
    if re.search(r"\b(hello|hi|hii+|hey|heyy+|salam|salaam|assalam|aslam|namaste|namaskar|hailo|aoa|sat sri akal|satsriakal|ki haal)\b", t):
        return r["hello"]
    if re.search(r"\b(how are you|how r u|kaise ho|kaise hain|kaisay ho|kese ho|kese hain|kya haal|ki haal|kida|kidda|ap kise hin|aap kaise hain)\b", t):
        return r["how"]
    if re.search(r"\b(what are you doing|what r u doing|kya kar rahe|kya kar rahi|ki kar rahe|ki kar rahi|ki karna|ki kardi|ki karde|tussi ki karde|tussi ki kar rahe)\b", t):
        return r["doing"]
    if re.search(r"\b(where are you|where r u|kahan ho|kidhar ho|tussi kithe|kithe o)\b", t):
        return r["where"]
    if re.search(r"\b(what is up|whats up|what's up|kya chal raha|ki haal chaal|ki haal aa)\b", t):
        return r["whatsup"]
    if re.search(r"\b(thank you|thanks|shukriya|bohat shukriya|meharbani|dhanvaad|dhanyavaad)\b", t):
        return r["thanks"]
    if re.search(r"\b(who are you|tum kon ho|tum kaun ho|aap kon|your name|naam kya|tu kaun)\b", t):
        return r["identity"]
    if re.search(r"\b(bored|boring|bore ho|pak gaya|pak gayi|vehla|vela)\b", t):
        return r["bored"]
    if re.search(r"\b(sad|udaas|pareshan|tension|mood off|dukhi|dil udaas)\b", t):
        return r["sad"]
    if re.search(r"\b(happy|khush|khushi|great news|vadiya khabar|changa hoya)\b", t):
        return r["happy"]
    if re.search(r"\b(joke|jokes|mazaak|mazak|chutkula|latifa|hasao|laugh)\b", t):
        return r["joke"]
    if re.search(r"\b(bye|goodbye|allah hafiz|khuda hafiz|see you|fer milange|chal bye)\b", t):
        return r["bye"]
    if re.fullmatch(r"(yes|yeah|yup|haan|han|ji|achha|acha|theek|ok|okay|bilkul|haaji)[!. ]*", t):
        prior = " ".join(normalize(x.get("content", "")) for x in history[-MAX_HISTORY:] if isinstance(x, dict))
        if "joke" in prior or "mazak" in prior:
            return r["joke"]
        return r["yes"]
    if re.fullmatch(r"(no|nahi|nahin|na|nope)[!. ]*", t):
        return r["no"]
    if re.search(r"\b(time kya|abhi time|abhi kitne? baje|kitna time|what time|current time|time now|waqt kya|abhi waqt|samay kya|kinna time)\b", t):
        now = india_now().strftime("%I:%M %p IST")
        return ("It is " if lang == "en" else "Hun time " if lang == "pa" else "Abhi time ") + now + "."
    if re.search(r"\b(today date|date today|aaj date|aaj ki date|date kya|what date|what is the date|today.s date|aaj ki tareekh|ajj di tareekh|tareekh kya|aaj ka din|which day is it|what day is it)\b", t):
        today = india_now().strftime("%A, %d %B %Y")
        return ("Today's date is " if lang == "en" else "Ajj di tareekh " if lang == "pa" else "Aaj ki tareekh ") + today + "."
    # Short conversational turns need context-sensitive replies, not the same
    # generic clarification on every message.
    words = set(t.split())
    if t in {"baby", "babu", "babyy", "oye baby", "oy baby", "hmm", "hmmm"}:
        if lang == "en":
            return "Hey, I'm here. What did you want to tell me?"
        if lang == "pa":
            return "Oye, dasso ji! Ki gall karni aa?"
        return "Haan bolo, kya baat hai?"
    if re.search(r"\b(axha|acha|achha|accha)\b", t) and re.search(r"\b(kya du|kia du|kya doon|kia doon|kya btaun|kya bataun|kya bolun)\b", t):
        if lang == "en":
            return "Just tell me what's on your mind. I'm listening."
        if lang == "pa":
            return "Jo dil vich aa, dass deo ji. Main sun rahi aan."
        return "Jo dil mein hai woh bata do, main sun rahi hoon."
    if t in {"hain", "hein", "han", "huh", "what", "kya", "kyaa", "acha", "achha", "axha"} or t.endswith("?") and len(t.split()) <= 2:
        previous_assistant = next((normalize(x.get("content", "")) for x in reversed(history) if isinstance(x, dict) and x.get("role") == "assistant"), "")
        if any(token in previous_assistant for token in ("context", "samajh", "listening", "batao", "tell me")):
            if lang == "en":
                return "I mean, you can talk to me normally. What's going on?"
            if lang == "pa":
                return "Mera matlab, tussi aam gall karo ji. Ki hoya?"
            return "Mera matlab, tum mujhse normal baat kar sakte ho. Kya chal raha hai?"
        if lang == "en":
            return "What happened? Tell me a little more."
        if lang == "pa":
            return "Ki hoya ji? Thoda hor dasso."
        return "Kya hua? Thoda aur batao na."
    # Contextual follow-up: resolve short replies against the previous turn.
    if history:
        last_user = next((normalize(x.get("content", "")) for x in reversed(history) if isinstance(x, dict) and x.get("role") == "user"), "")
        previous_assistant = next((normalize(x.get("content", "")) for x in reversed(history) if isinstance(x, dict) and x.get("role") == "assistant"), "")
    # Give unresolved contextual messages one bounded model attempt before
    # the generic local fallback. Deterministic intents above stay local, while
    # short follow-ups and open chat can use the supplied conversation history.
    model_reply = local_model_generate(raw, history)
    if model_reply:
        return model_reply

    # Do not let a generic short-follow-up rule swallow real questions.
    # The model must see the recent dialogue so it can resolve "why?", "meaning?",
    # references, and incomplete Roman Urdu messages from their actual context.
    # The model was already tried before deterministic fallback rules above.
    # Only use a clarification fallback when no generative model is configured
    # or the provider is temporarily unavailable.
    if lang == "en":
        return "I'm not fully sure what you mean yet. Can you give me one small hint?"
    if lang == "en":
        return "I'm listening. Tell me a little more about what you mean."
    if lang == "pa":
        return "Haanji, thoda hor dasso. Main samajh rahi aan."
    return "Haan, main sun rahi hoon. Aage batao?"

class Handler(BaseHTTPRequestHandler):
    def send_json(self, status, payload):
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def authorized(self):
        return not API_TOKEN or hmac.compare_digest(self.headers.get("Authorization", ""), "Bearer " + API_TOKEN)

    def do_GET(self):
        if self.path.split("?", 1)[0].rstrip("*") == "/health":
            model_status = model_runtime_status()
            return self.send_json(200, {"ok": True, "service": "alexa-own-ai-micro", "engine": "multilingual-context+llm" if model_status["is_llm"] else "multilingual-local-context", "is_llm": model_status["is_llm"], "model_provider": model_status["provider"], "model": model_status["model"], "languages": ["English", "Roman Urdu", "Roman Hindi", "Roman Punjabi", "Urdu script input", "Devanagari input"], "memory_mode": "sqlite_last_20"})
        return self.send_json(200, {"service": "alexa-own-ai-micro", "endpoints": ["/health", "/chat", "/v1/chat/completions"]})

    def do_POST(self):
        if not self.authorized():
            return self.send_json(401, {"error": "unauthorized"})
        if self.path not in ("/chat", "/v1/chat/completions"):
            return self.send_json(404, {"error": "not_found"})
        try:
            size = int(self.headers.get("Content-Length", "0"))
            if size <= 0 or size > MAX_BODY:
                return self.send_json(413 if size > MAX_BODY else 400, {"error": "request_too_large" if size > MAX_BODY else "empty_body"})
            body = json.loads(self.rfile.read(size) or b"{}")
            if not isinstance(body, dict):
                raise ValueError()
        except (ValueError, json.JSONDecodeError):
            return self.send_json(400, {"error": "invalid_json"})

        if self.path == "/chat":
            message = str(body.get("message", ""))
            supplied = body.get("history", [])
            if not isinstance(supplied, list): supplied = []
            conversation = str(body.get("conversation_id") or body.get("chat_id") or body.get("user_id") or body.get("session_id") or "").strip()
            history = get_history(conversation, supplied, message)
            reply = response_for(message, history)
            save_turns(conversation, message, reply)
            model_status = model_runtime_status()
            return self.send_json(200, {"reply": reply, "fallback": False, "provider": model_status["provider"] or "alexa-own-ai-micro", "is_llm": model_status["is_llm"], "model": model_status["model"], "language": detect_language(message), "memory_messages": min(MAX_HISTORY, len(history)+2)})

        messages = body.get("messages", [])
        if not isinstance(messages, list): messages = []
        message = ""
        for item in reversed(messages):
            if isinstance(item, dict) and item.get("role") == "user":
                content = item.get("content", "")
                message = content if isinstance(content, str) else ""
                break
        conversation = str(body.get("conversation_id") or body.get("chat_id") or body.get("user_id") or body.get("session_id") or "").strip()
        history = get_history(conversation, messages, message)
        reply = response_for(message, history) or REPLIES[detect_language(message)]["unknown"]
        # Always return a non-empty assistant message as a completed turn.
        save_turns(conversation, message, reply)
        return self.send_json(200, {
            "id": "alexa-own-ai-micro-v2", "object": "chat.completion",
            "choices": [{"index": 0, "message": {"role": "assistant", "content": reply}, "finish_reason": "stop"}]
        })

    def log_message(self, fmt, *args):
        print("%s - %s" % (self.address_string(), fmt % args), flush=True)

if __name__ == "__main__":
    print("Alexa Own AI microservice listening on port", PORT, flush=True)
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()
