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

# Progress and "this area is done" (backend/live/guide.py). $n = books found so far.
PROGRESS = "That is $n books found so far. Keep going."
IDLE = ["That area looks done. Move on to the next shelf, or say finish when you have covered everything.",
        "Nothing new for a while. Say finish when you are done."]

# Before the sweep (backend/live/guide.py, setup): camera focus first, then the room size, then the sweep.
SETUP = {
    "focus": ["First, let's get the camera sharp. Point it at the nearest shelf and hold still for two seconds.",
              "Hold the phone still on one shelf until the picture is sharp."],
    "room": ["Sharp, good. Now the room size: tap Measure the room, aim the circle at each floor corner in turn, and tap Add point.",
             "Tap Measure the room on the screen and mark the floor corners."],
    "start_measured": "Room measured: $area square metres. Now walk the room once, slowly, shelf by shelf.",
    "start_skipped": "We will leave the room size blank. Now walk the room once, slowly, shelf by shelf.",
    "start_no_ar": "Sharp, good. This phone cannot measure the room, so the room size stays blank. Now walk the room once, slowly, shelf by shelf.",
}
