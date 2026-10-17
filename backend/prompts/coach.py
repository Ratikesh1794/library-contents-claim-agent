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

# What to say about books in view that could not be read. $what = "the books on the left" (backend/live/guide.py).
BOOK_ASKS = {
    "hold": "Hold steady on $what for a second.",
    "closer": "Move a bit closer to $what.",
    "glare": "Tilt the phone a little to cut the glare on $what.",
    "dark": "It is too dark for $what. Can you turn on a light?",
    "blocked": "Something is in front of $what. Move it, or shift to the side.",
    "partial": "Pan a little further so $what is fully in view.",
}
GIVE_UP = "I could not read $what. I will note it and move on."

# Going back to books that were passed unread (backend/live/guide.py). $what = "the book" / "two books"; $where = "on your left".
BACK_ASK = "I could not read $what you passed $where. Go back and hold steady on $it."
BACK_WHERE = {"left": "on your left", "right": "on your right", "above": "higher up", "below": "lower down"}
