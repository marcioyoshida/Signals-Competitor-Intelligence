

# --- #150: alias patterns are compiled once ------------------------------------------

def test_word_match_compiles_each_alias_pattern_once():
    """The pattern used to be handed to `re.search` as a string, leaning on the `re` module's
    512-entry internal cache. With an 11k-entity registry that cache thrashed and every alias
    recompiled on every resolve — 89% of a resolve_entities call was the regex compiler.
    """
    from src.synth import entities as E
    E._word_re.cache_clear()
    for _ in range(50):
        assert E._word_match("BRADESCO", " BCO BRADESCO S.A. ") is True
        assert E._word_match("STONE", " STONEX DTVM ") is False      # boundary still anchored
    info = E._word_re.cache_info()
    assert info.currsize == 2          # two distinct tokens, two compilations
    assert info.misses == 2
    assert info.hits == 98
