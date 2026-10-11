# Demo Instagram feed

`watcher.py` treats each folder in here as a club's Instagram feed. The folder name must be
the club's Instagram handle from `clubs.csv` (e.g. `thehackru`, `ruamsa`).

A "post" is an image and/or a caption with the same name:

```
data/instagram_demo/thehackru/
    workshop.png    <- the flyer (png, jpg or webp)
    workshop.txt    <- the caption (optional)
```

Drop a new flyer in while `watcher.py` is running and it's picked up on the next check
(every 60 seconds by default), read by Gemini, and added to the calendar automatically.
Each post is only read once.
