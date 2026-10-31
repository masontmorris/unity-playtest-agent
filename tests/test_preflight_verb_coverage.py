from playtest.roles.preflight import MAX_ACTIONS, _BODY


def test_actions_prompt_prioritizes_verbs_and_unknown_combinations():
    prompt = _BODY["actions"]
    assert MAX_ACTIONS == 18
    assert "Cover distinct verbs the input code reads before adding a second device" in prompt
    assert "use one device per verb unless the source shows devices behave differently" in prompt
    assert "reader fields and the view, not only displacement" in prompt
    assert "without moving the entity is still an action a playtester needs" in prompt
    assert "Compose modifier chords" in prompt
    assert "verbs held during a state" in prompt
    assert "Their effects are unknown until measured" in prompt
    assert "each look axis on one look device" in prompt
    assert "never imply it was measured" in prompt
    assert all(word not in prompt.lower() for word in ("fpskit", "dragonblaze", "street-racing"))


def test_actions_prompt_says_a_non_moving_button_needs_a_tested_hold():
    prompt = _BODY["actions"]
    assert "a button calibration saw change fields or the view but not move the entity has none" in prompt
    assert "A null hold with no measured minimum leaves the declaration unexecutable" in prompt
    assert "the control that leaves the ground" in prompt
