"""Notifications: who is told when something needs attention, and how.

A recipient is one email address or one webhook URL, and chooses for itself which events it
receives (`model`). The configuration and the state that keeps a restart from repeating
itself live under `manager/` (`store`); `detect` finds the conditions that call for a
notification, `delivery` carries one out and `service` is the background task that ties them
together.
"""
