# Task: refactor_tag_list

Harden `refactor_tag_list(tags)` in `seed.py`. The legacy helper is being
consolidated before it is reused by import and search code. Establish a clear
normalization contract: equivalent spelling must not create duplicate tags, and
malformed input must not silently enter the shared tag index.
