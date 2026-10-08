"""Embedding files into a collection through the ingester (`qdrant-ingest`).

* `catalog`: the managed jobs in the ingester's `jobs.yaml`, and how they are written
* `documents`: browsing and selecting inside the documents folder, jailed to it
* `uploads`: the staging folders of uploaded files, which are removed after a run
* `client`: the ingester's REST control plane
* `runs`: starting a run, following it, and the clean-up after it
* `watcher`: the background task that applies the clean-up when nobody has the page open
"""
