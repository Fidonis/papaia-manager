// The core modules every page loads (base.html). Page modules live under ../pages/<group>.js
// and import what they need from here, e.g. readIsland from ./islands.js.
import { installPollGuard } from "./poll.js";

installPollGuard(document);
