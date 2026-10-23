"""Admin HTTP APIs of the core proxy: proxy keys (``keys``, ST-9) and the admin-managed
routing and team-limits overlay (``routing``, ST-10). main.py owns the routes and their
authentication; these modules hold the logic, so they can be tested without the app."""
