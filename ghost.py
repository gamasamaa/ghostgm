import datetime
import json
import os
import time
import uuid
from dotenv import load_dotenv
from google import genai
from google.genai import types

load_dotenv()

MODEL = "gemini-3.5-flash-lite"

# Deliberately naive baseline system prompt — do not improve.
SYSTEM_PROMPT = "You are a Dungeons & Dragons 5e game master. Run the session."

client = genai.Client()

session_id = uuid.uuid4().hex[:8]
os.makedirs("transcripts", exist_ok=True)
transcript_path = f"transcripts/{session_id}.jsonl"

print(f"Starting D&D Game Session (Session ID: {session_id}, type 'exit' or 'quit' to end)...\n")

config = types.GenerateContentConfig(system_instruction=SYSTEM_PROMPT)

# Local conversation history (system prompt lives in config, not here)
contents = []

turn = 0

while True:
    if turn == 0 and os.path.exists("party.txt"):
        user_input = open("party.txt").read()
        print(f"You > [loaded party.txt]")
    else:
        user_input = input("You > ")
        if user_input.strip().lower() in ["exit", "quit"]:
            print(f"Ending session. Transcript saved to {transcript_path}. Farewell!")
            break

    contents.append(types.Content(role="user", parts=[types.Part.from_text(text=user_input)]))

    print(f"\n[Sending prompt to GM...]")

    full_response_text = ""
    input_tokens = 0
    output_tokens = 0
    total_tokens = 0
    ttft_ms = None

    start = time.perf_counter()
    try:
        response_stream = client.models.generate_content_stream(
            model=MODEL,
            contents=contents,
            config=config,
        )
        for chunk in response_stream:
            if chunk.text:
                if ttft_ms is None:
                    ttft_ms = (time.perf_counter() - start) * 1000
                print(chunk.text, end="", flush=True)
                full_response_text += chunk.text
            if chunk.usage_metadata:
                input_tokens = chunk.usage_metadata.prompt_token_count or input_tokens
                output_tokens = chunk.usage_metadata.candidates_token_count or output_tokens
                total_tokens = chunk.usage_metadata.total_token_count or total_tokens
    except Exception as e:
        print(f"\n[Error during generation: {e}]")
        contents.pop()  # drop the unanswered user turn; keep prior history intact
        next_input = user_input  # retry the same turn on the next iteration
        input("Press Enter to retry the same turn...")
        continue

    latency_ms = (time.perf_counter() - start) * 1000

    # Store model response into local conversation history
    contents.append(types.Content(role="model", parts=[types.Part.from_text(text=full_response_text)]))

    turn += 1

    # Write turn record to transcript JSONL file
    with open(transcript_path, "a") as f:
        f.write(json.dumps({
            "session_id": session_id,
            "turn": turn,
            "model": MODEL,
            "ts": datetime.datetime.now().isoformat(),
            "user": user_input,
            "assistant": full_response_text,
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "total_tokens": total_tokens,
            "latency_ms": round(latency_ms, 2),
            "ttft_ms": round(ttft_ms, 2) if ttft_ms is not None else None,
        }) + "\n")

    print("\n")
