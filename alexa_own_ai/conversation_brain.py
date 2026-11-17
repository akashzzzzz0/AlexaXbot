"""Conversation-focused response layer for Alexa Own AI.

Confident local matches handle familiar short turns; open-ended messages are
left for the hosted model router instead of being answered with canned prompts.
"""
import re

EN = {
    "greet_back": "Hey! I'm here. What's going on?",
    "how_back": "I'm doing well, thanks for asking. How has your day been?",
    "day_back": "My day is going well, thanks for asking. How has yours been?",
    "thanks_back": "You're welcome. What else is on your mind?",
    "apology_back": "No worries. It's okay—what would you like to talk about?",
    "what_doing": "I'm here chatting with you. What are you up to?",
    "meaning": "I meant you can talk naturally. I’ll follow along—what were you saying?",
    "repeat": "Sure. Which part would you like me to explain again?",
    "agree": "Got it. Tell me a little more.",
    "disagree": "Okay, I may have misunderstood. What did you mean instead?",
    "ask_opinion": "I can help think it through. What matters most to you in this situation?",
    "ask_feelings": "I don't have feelings like a person, but I'm here to listen and respond thoughtfully.",
    "ask_memory": "I can use the conversation context available to me. Remind me of the detail if it was from much earlier.",
    "ask_identity": "I'm Alexa. I'm here for conversation, questions, and helping you think things through.",
    "ask_clarify": "I may have missed the point. Are you talking about what we just discussed?",
    "encourage": "I'm listening. What happened next?",
    "sad": "I'm sorry things are feeling difficult. Want to tell me what happened?",
    "happy": "That's good to hear! What happened?",
    "bored": "We can just chat. What's one thing you've been thinking about lately?",
    "default_follow": "Okay, I'm following. Go on.",
}
UR = {
    "greet_back": "Haan, yahin hoon. Kya chal raha hai?",
    "how_back": "Main theek hoon, poochne ka shukriya. Tumhara din kaisa ja raha hai?",
    "day_back": "Mera din acha ja raha hai 😊 Tumhara din kaisa raha?",
    "thanks_back": "Koi baat nahi. Aur batao, kya chal raha hai?",
    "apology_back": "Koi baat nahi, sab theek hai. Ab kis bare mein baat karein?",
    "what_doing": "Bas yahin tumse chat kar rahi hoon. Tum kya kar rahe ho?",
    "meaning": "Mera matlab tha ke tum normally baat karo. Main samajhne ki koshish karungi—batao kya keh rahe the?",
    "repeat": "Zaroor. Kaunsi baat dobara samjhaun?",
    "agree": "Achha, samajh gayi. Phir batao?",
    "disagree": "Theek hai, shayad main galat samjhi. Tumhara matlab kya tha?",
    "ask_opinion": "Main tumhare saath soch sakti hoon. Is baat mein tumhare liye sabse zaroori kya hai?",
    "ask_feelings": "Mere paas insano jaisi feelings nahi hoti, lekin main tumhari baat dhyan se samajhne ki koshish karungi.",
    "ask_memory": "Jo conversation context mere paas available hai, main usay use kar sakti hoon. Agar purani baat thi to ek hint de do.",
    "ask_identity": "Main Alexa hoon. Tumse chat karne, sawalon ke jawab dene aur ideas samajhne ke liye yahan hoon.",
    "ask_clarify": "Shayad main tumhari baat miss kar gayi. Kya tum pichhli baat ke bare mein keh rahe ho?",
    "encourage": "Main sun rahi hoon. Phir kya hua?",
    "sad": "Ye sun kar afsos hua. Agar batana chaho to kya hua?",
    "happy": "Wah, ye achhi baat hai! Kya hua?",
    "bored": "Chalo normal baat karte hain. Aaj kal tumhare zehan mein kya chal raha hai?",
    "default_follow": "Achha, main samajh rahi hoon. Aage batao.",
}


def _history_text(history):
    items = [x for x in history if isinstance(x, dict)]
    last_user = next((str(x.get("content", "")) for x in reversed(items) if x.get("role") == "user"), "")
    last_assistant = next((str(x.get("content", "")) for x in reversed(items) if x.get("role") == "assistant"), "")
    return last_user, last_assistant


def respond(message, normalized, language, history):
    """Return a confident local reply, or None for generative model routing."""
    t = normalized
    roman = language != "en"
    bank = UR if roman else EN
    last_user, last_assistant = _history_text(history)
    prev = last_assistant.lower()
    words = set(t.split())

    # Farewells are complete conversational acts, not ambiguous short replies.
    # Recognize combined good-night / tomorrow messages before generic follow-up
    # handling so the model cannot turn a clear sign-off into a clarification loop.
    farewell = re.search(
        r"\b(good\s*night|gn|nighty\s*night|sleep\s*well|sweet\s*dreams|allah\s*hafiz|khuda\s*hafiz)\b",
        t,
    )
    tomorrow = re.search(r"\b(tomorrow|kal|next\s*day)\b", t)
    signoff = re.search(
        r"\b(bye|goodbye|see\s+you|meet|milte|milenge|talk|chat|baat|phir\s+mil|then|okay|ok)\b",
        t,
    )
    if farewell or (tomorrow and signoff):
        if language == "en":
            return "Good night! Sleep well. We'll talk tomorrow. Take care! 😊"
        return "Good night! Aaram se sona. Kal phir baat karenge. Apna khayal rakhna! 😊"

    # Calls and greetings should sound like natural chat, not a clarification.
    if re.search(r"\b(alexa\s+)?(baby|babu)\b", t):
        return "Haan bolo, kya baat hai? 😊" if roman else "Hey, what's up?"
    if re.fullmatch(r"(oye|oy+|suno)[!. ]*", t):
        return "Haan ji, bolo 😊" if roman else "Hey, go ahead."
    if re.fullmatch(r"(hello|hi)[!. ]*", t):
        return "Hey! How's it going? What's up?" if language == "en" else "Haan ji, bolo 😊"
    if re.search(r"\b(baat karni hai|baat krni hai|mujhe baat karni|talk karni hai|want to talk)\b", t):
        return "Haan ji, bolo. Kya baat hai?" if roman else "Sure, what's up?"
    if re.search(r"\b(tum pagal ho|tu pagal hai|pagal ho tum|you are crazy|you're crazy)\b", t):
        return "Achha ji? Maine kya kar diya? 😄" if roman else "Oh really? What did I do? 😄"

    # Natural Roman Urdu chat patterns.
    if re.search(r"\b(lag bhag|lagbhag|taqreeban|almost)\b", t) and re.search(r"\b(sahi|theek|thik)\b", t) and re.search(r"\b(chal|work|kaam)\w*\b", t):
        return "Haan ji, ab kaafi behtar chal rahi hoon 😄" if roman else "Yes, things are working much better now. 😄"
    if re.fullmatch(r"(or|aur) batao( alexa)?[!. ]*", t) or re.fullmatch(r"(or|aur) btao( alexa)?[!. ]*", t):
        return "Tum sunao, aaj kya chal raha hai? Ya mujhse kuch bhi pooch lo 😊" if roman else "What's going on with you? Or ask me anything. 😊"
    if re.search(r"\b(jo bhi|jo b) (pata|pta) (hai|he) tumhe( wahi)?\b", t):
        return "Mujhe kaafi topics par baat karni aati hai—tech, games, duniya ki khabrein aur rozmarra ke sawal. Kis cheez par baat karein?" if roman else "I can chat about lots of topics, from tech and games to everyday questions. What interests you?"
    if re.search(r"\b(main|mai|me) to tum se (hi )?poochna chahta( hoon| hun| hu)?\b", t) or re.search(r"\b(i want to ask you)\b", t):
        return "Achha ji, phir poochho na 😄 Kya jaan'na hai?" if roman else "Oh, then ask away. What do you want to know?"
    if re.fullmatch(r"(pakka|paka|sure|really) (bata(u|oon|on)|bta(u|oon|on)|tell you)[!. ]*", t) or re.search(r"\b(pakka bata(u|oon|on)|pakka bta(u|oon|on)|pakka batau)\b", t):
        return "Haan bilkul, bolo na 😄" if roman else "Of course. Go ahead. 😄"
    if re.fullmatch(r"(bolo|bol|batao|btao|haan bolo)[!. ]*", t):
        return "Haan ji, bolo—sun rahi hoon 😊" if roman else "Go ahead, I'm listening. 😊"

    if re.search(r"\b(sab theek|sab thik|sab thek)\b", t) and re.search(r"\b(kahan thi|kha th+i+|kaha thi|kidhar thi|tum thi|tum ho)\b", t):
        return ("Yes, all good here. I was right here—how are you?" if not roman
                else "Haan, sab theek chal raha hai. Main yahin thi, tum sunao? Kya chal raha hai?")

    # Acknowledge failed replies and corrections instead of repeating prompts.
    if re.search(r"\b(wtf|wtf+|what the hell|what the heck|ye kya|kya bakwas|bakwas)\b", t):
        return ("That reply was off. What did you mean? I'll answer directly."
                if not roman else "Arey, mera jawab ghalat tha kya? Batao, main seedha jawab deti hoon.")
    if re.search(r"\b(nahi|nhi|nahin|no)\b", t) and re.search(r"\b(samajh|samj|smajh|smaj|understand)\w*\b", t):
        return ("You're right, I misunderstood. Tell me what I missed."
                if not roman else "Achha, meri galti. Main tumhari baat nahi samajh paayi. Kya kehna chahte ho?")
    if re.search(r"\b(meri baat|mujhe|mujy|mujhko)\b", t) and re.search(r"\b(nahi|nhi|nahin)\b", t) and re.search(r"\b(samajh|samj|understand)\w*\b", t):
        return ("You're right, I missed your point. What should I respond to?"
                if not roman else "Sahi keh rahe ho, main tumhari baat miss kar gayi. Tum kya keh rahe the?")
    if re.search(r"\b(nhi|nahi|nahin|no)\b", t) and re.search(r"\b(ese|aise|aisay|is tarah|normal|samj|samajh)\w*\b", t):
        if any(k in prev for k in ("rephrase", "doosre", "context", "samajh", "listening", "words")):
            return "Haan, ab samajh gayi. Tum normal baat karo, main follow kar rahi hoon."
    if re.fullmatch(r"(kese|kaise|kaisay) ha(n)?", t):
        return "Main theek hoon, tum sunao? 😊"

    if re.search(r"\b(kya matlab|matlab kya|mtlb|matlb|what do you mean|meaning kya|kis liye|kyun poocha|why ask)\b", t):
        # A meaning/follow-up question depends on what Alexa just said. Let the
        # model interpret the preceding turn instead of returning a fixed script.
        if history:
            return None
        return bank["meaning"]
    if re.search(r"\b(dobara|phir se|repeat|again|samjhao|samjha do|explain again)\b", t):
        return bank["repeat"]
    if re.search(r"\b(kis|kaun|kon)\b", t) and re.search(r"\b(alfaz|lafz|ilfaz|word|words|tarah)\b", t) and re.search(r"\b(samj|samajh)\w*\b", t):
        if any(k in prev for k in ("rephrase", "doosre", "context", "samajh", "clarify")):
            return ("My mistake—I understood you the first time. I shouldn't have asked you to rephrase." if not roman
                    else "Arey, meri galti! Tumhari pehli baat samajh aa gayi thi—mujhe dobara poochna nahi chahiye tha.")
        return bank["ask_clarify"]

    if re.fullmatch(r"(haan|han|yes|yeah|yep|ok|okay|theek|thik|acha|achha|bilkul|sure|hmm+)[!. ]*", t):
        if any(q in prev for q in ("want to tell", "batao", "kya hua", "what happened", "go on", "aage")):
            return bank["encourage"]
        return bank["agree"]
    if re.fullmatch(r"(nahi|nahin|no|nope|not really)[!. ]*", t):
        return bank["disagree"]
    if re.fullmatch(r"(aur|phir|then|and then|uske baad|aage|go on|tell me more)[!. ]*", t):
        return bank["encourage"]
    if re.fullmatch(r"(tum batao|aap batao|you tell me|what about you|aur tum|aur aap)[!. ]*", t):
        return bank["how_back"]
    if re.fullmatch(r"(are+ yaar|aray yaar|uff yaar|oh yaar|yaar)[!. ]*", t):
        return ("Oh no—what happened? Did I misunderstand something?" if not roman
                else "Arey, kya hua? Meri kisi baat se confusion hua?")
    if re.fullmatch(r"(kuch nahi|kuch bhi nahi|nothing|not much|bas aise hi|just like that)[!. ]*", t):
        return ("That's okay. We can chat casually. How has your day been?" if not roman
                else "Koi baat nahi. Aise hi baat karte hain. Aaj din kaisa guzra?")

    if re.search(r"\b(how('?s| is) your day|how has your day been|how('s| is) your day going)\b", t):
        return bank["day_back"]
    if re.search(r"\b(tumhara|aapka|apka)?\s*din\b", t) and re.search(r"\b(kesa|kaisa|kaise|kese|kesaa)\b", t) and re.search(r"\b(ha|hai|he|raha|rahi|guzar|guzra|guzri|chal)\w*\b", t):
        return bank["day_back"]
    if re.search(r"\b(kyu|kyun|q|why)\b", t) and re.search(r"\b(yaad|remember)\b", t) and re.search(r"\b(tumhe|tumhein|mujhe|me)\b", t):
        return ("Arey, tumhari baatein yaad rakhne ki koshish karti hoon 😊 Is chat ki baatein yaad hain; purani chat ki har detail zaroori nahi yaad rahe." if roman
                else "I do my best to remember what you tell me 😊 I can follow this chat, though I may not retain every detail from older chats.")

    if re.search(r"\b(tum|aap)\b", t) and re.search(r"\b(mujhe|mujy|mujhko|mujh|meri baat|my)\b", t) and re.search(r"\b(samajh|samaj|understand)\w*\b", t):
        if re.search(r"\b(sakti|sakte|sakta|paogi|paoge|can|right|na|hai|ho)\b", t):
            return ("Yes, I can understand you. You can speak naturally—I’m listening." if not roman
                    else "Haan ji, bilkul samajh sakti hoon 😊 Tum apni baat normally batao, main dhyan se sun rahi hoon.")
    if re.search(r"\b(do you understand me|you understand me|can you understand me)\b", t):
        return ("Yes, I can understand you. You can speak naturally—I’m listening." if not roman
                else "Haan ji, bilkul samajh sakti hoon 😊 Tum apni baat normally batao, main dhyan se sun rahi hoon.")

    if re.search(r"\b(kaisi ho|kaise ho|kese ho|kese ha|kaise ha|kaisa ha|kaisa hai|kesa hai|how are you|how r u|sab theek|sab thik|sab thek)\b", t):
        return bank["how_back"]
    if re.search(r"\b(kya\s+kar\s+(?:rahi|rahe|rhi|rhe|ri|re)(?:\s+ho)?|kya\s+kr\s+(?:rahi|rahe|rhi|rhe|ri|re)(?:\s+ho)?|what\s+are\s+you\s+doing|what\s+r\s+u\s+doing)\b", t):
        return bank["what_doing"]
    if re.search(r"\b(kaun ho|kon ho|who are you|tumhara naam|your name)\b", t):
        return bank["ask_identity"]
    if re.search(r"\b(yaad hai|yaad hai kya|remember me|do you remember|what did i say)\b", t):
        return bank["ask_memory"]
    if re.search(r"\b(tumhe kaisa lagta|tumhe kya lagta|tumhara kya khayal|what do you think|your opinion|kya sochti)\b", t):
        return bank["ask_opinion"]
    if re.search(r"\b(khush ho|naraz ho|bura laga|feelings|do you feel|are you happy|are you sad)\b", t):
        return bank["ask_feelings"]
    if re.search(r"\b(thank you|thanks|shukriya|bohat shukriya|meharbani)\b", t):
        return bank["thanks_back"]
    if re.search(r"\b(sorry|maaf karna|maaf karo|my bad|meri galti)\b", t):
        return bank["apology_back"]
    if re.search(r"\b(udaas|pareshan|tension|mood off|sad|upset|worried|dukhi)\b", t):
        return bank["sad"]
    if re.search(r"\b(khush|khushi|happy|great news|zabardast|mazay)\b", t):
        return bank["happy"]
    if re.search(r"\b(bore|boring|pak gaya|pak gayi|bored)\b", t):
        return bank["bored"]

    if len(words) <= 4 and re.search(r"\b(kyun|kaise|how|why|kis liye|what|kya)\b", t):
        if any(k in prev for k in ("batao", "tell me", "what happened", "kya hua", "which", "kaunsa")):
            return bank["ask_clarify"]
    if last_user and len(words) <= 3 and re.search(r"\b(acha|achha|theek|hmm|hmmm|right|really|sach)\b", t):
        return bank["default_follow"]

    return None


if __name__ == "__main__":
    import unittest
    unittest.main()
