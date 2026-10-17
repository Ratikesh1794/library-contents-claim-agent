"""What the voice agent says for each capture problem (backend/live/coach.py). Placeholder: $region ("shelf row 2")."""

ASKS = {
    "too_far": "Step a bit closer to $region.",
    "blur": "Hold steady for a second.",
    "glare": "Tilt the phone or step to the side.",
    "too_dark": "Can you turn on a light?",
    "blocked": "Could you move the object in front, or shift so I can see past it?",
    "partial": "Pan a bit further so $region is fully in view.",
}

# Proactive guidance (backend/live/guide.py). A list is escalating wording: the 2nd message in a row is firmer.
GUIDE_ASKS = {
    "slow": ["Slow down a little. Pause on each shelf for a second.",
             "Still too blurry. Stop for a moment and hold the phone still."],
}
