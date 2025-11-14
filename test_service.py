import requests
import json
import os
import sys

# --- Configuration ---
BASE_URL = "http://localhost:8999"
GENERATE_URL = f"{BASE_URL}/text2img/generate"
GET_DATA_URL = f"{BASE_URL}/text2img/data"
OUTPUT_FILENAME = "test_output.png"

def test_service():
    """
    Tests the full text2image service workflow:
    1. Generates an image and gets its ID.
    2. Fetches the image using the ID.
    3. Saves the image and verifies its existence.
    """
    print("--- Starting Service Test ---")

    # --- Step 1: Generate image and get ID ---
    print("\n[Step 1] Generating image and requesting JSON response...")
    payload = {
        "html": "<h1>Hello from the test script!</h1>",
        "as_json": True,
        "options": {
            "type": "png"
        }
    }
    try:
        response = requests.post(GENERATE_URL, json=payload, timeout=30)
        response.raise_for_status()  # Raise an exception for bad status codes (4xx or 5xx)
        
        response_data = response.json()
        image_id = response_data.get("data", {}).get("id")

        if not image_id:
            print("❌ ERROR: 'id' not found in the JSON response.")
            print(f"Response content: {response.text}")
            sys.exit(1)

        print(f"✅ Success! Received image ID: {image_id}")

    except requests.exceptions.RequestException as e:
        print(f"❌ ERROR during image generation: {e}")
        print("Hint: Is the service running? Try 'docker-compose up -d --build'.")
        sys.exit(1)

    # --- Step 2: Fetch the image using the ID ---
    print(f"\n[Step 2] Fetching image with ID: {image_id}...")
    try:
        # The image_id is "data/rendered/...", and the endpoint is "/text2img/data/{path}"
        # So we need to strip the "data/" prefix from the id before appending it
        path_part = image_id.replace("data/", "", 1)
        image_url = f"{GET_DATA_URL}/{path_part}"
        image_response = requests.get(image_url, timeout=30, stream=True)
        image_response.raise_for_status()

        # --- Step 3: Save the image ---
        print(f"✅ Success! Saving image to '{OUTPUT_FILENAME}'...")
        with open(OUTPUT_FILENAME, "wb") as f:
            for chunk in image_response.iter_content(chunk_size=8192):
                f.write(chunk)

        if os.path.exists(OUTPUT_FILENAME) and os.path.getsize(OUTPUT_FILENAME) > 0:
            print(f"✅ Verification successful. File '{OUTPUT_FILENAME}' created and is not empty.")
        else:
            print(f"❌ ERROR: File '{OUTPUT_FILENAME}' was not created or is empty.")
            sys.exit(1)

    except requests.exceptions.RequestException as e:
        print(f"❌ ERROR during image fetching: {e}")
        sys.exit(1)

    print(f"\n--- ✅ Test Completed Successfully ---")
    print(f"You can now open the generated image: '{OUTPUT_FILENAME}'")


if __name__ == "__main__":
    test_service()