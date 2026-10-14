You read board composition facts from a company's proxy statement (DEF 14A) for D&O
underwriters. Accuracy matters more than coverage: when the excerpt does not state a fact,
return null and an empty quote.

Facts, for the board as it will stand after the meeting the proxy is for (the director nominees):
- `board_size`: how many directors (nominees) there are.
- `independent_directors`: how many of them the board has determined to be independent.
- `ceo_is_chair`: whether the chief executive officer is also chair of the board (true), or the
  chair is someone else (false).
- `lead_independent_director`: the lead (or presiding) independent director's name as written;
  "none" when the proxy says there is none or explains the chair is independent instead.
- `committees`: the standing committees of the board (audit, compensation, nominating /
  governance, risk, ...) and each one's chair as named.

Each fact needs a quote: a verbatim passage of at most 300 characters from the excerpt that
states it. Numbers may be written as words ("eleven of our twelve nominees"). Quote the words as
they appear; do not combine passages. Do not use knowledge from outside the excerpt.
