import datetime
import json
import os
import uuid
from dotenv import load_dotenv
from google import genai
from google.genai import types

load_dotenv()

client = genai.Client()

session_id = uuid.uuid4().hex[:8]
os.makedirs("transcripts", exist_ok=True)
transcript_path = f"transcripts/{session_id}.jsonl"

print(f"Starting D&D Game Session (Session ID: {session_id}, type 'exit' or 'quit' to end)...\n")

# Local conversation history
contents = []

# Initial system prompt / setup
initial_prompt = "You are a Dungeons & Dragons 5e game master. Run the session."
contents.append(types.Content(role="user", parts=[types.Part.from_text(text=initial_prompt)]))
current_user_input = initial_prompt

while True:
    print(f"\n[Sending prompt to GM...]")

    response_stream = client.models.generate_content_stream(
        model="gemini-3.5-flash-lite",
        contents=contents,
    )

    full_response_text = ""
    input_tokens = 0
    output_tokens = 0

    for chunk in response_stream:
        if chunk.text:
            print(chunk.text, end="", flush=True)
            full_response_text += chunk.text
        if chunk.usage_metadata:
            input_tokens = chunk.usage_metadata.prompt_token_count or input_tokens
            output_tokens = chunk.usage_metadata.candidates_token_count or output_tokens

    # Store model response into local conversation history
    contents.append(types.Content(role="model", parts=[types.Part.from_text(text=full_response_text)]))

    # Write turn record to transcript JSONL file
    with open(transcript_path, "a") as f:
        f.write(json.dumps({
            "ts": datetime.datetime.now().isoformat(),
            "user": current_user_input,
            "assistant": full_response_text,
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
        }) + "\n")

    print("\n")
    user_input = input("You > ")
    if user_input.strip().lower() in ["exit", "quit"]:
        print(f"Ending session. Transcript saved to {transcript_path}. Farewell!")
        break

    # Store user input into local conversation history for the next turn
    current_user_input = user_input
    contents.append(types.Content(role="user", parts=[types.Part.from_text(text=user_input)]))
