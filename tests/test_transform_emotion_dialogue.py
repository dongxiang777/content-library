from scripts.transform_emotion_dialogue import transform_dialogue


def test_merges_interleaved_speakers_by_inferred_role_and_keeps_content_order():
    transcript = (
        "【说话人1】：大家好，欢迎来到直播间。\n"
        "【说话人2】：我以前总是控制孩子，他现在不爱学习。\n"
        "【说话人3】：以前你是怎么做的？\n"
        "【说话人2】：我越管他，他越叛逆。\n"
        "【说话人3】：家长要用慈悲心对待孩子，也请大家点个关注。"
    )

    result = transform_dialogue(transcript)

    assert result.status == "auto"
    assert result.text == (
        "用户：我以前总是控制孩子，他现在不爱学习。\n"
        "我越管他，他越叛逆。\n"
        "主播：大家好，欢迎来到直播间。\n"
        "以前你是怎么做的？\n"
        "家长要用慈悲心对待孩子，也请大家点个关注。"
    )


def test_does_not_automatically_merge_multiple_possible_users():
    transcript = (
        "【说话人1】：欢迎来到直播间。\n"
        "【说话人2】：我和我老公最近总吵架。\n"
        "【说话人3】：我也遇到过类似的问题。\n"
        "【说话人1】：今天我们来聊聊这个话题。"
    )

    result = transform_dialogue(transcript)

    assert result.status == "review"
    assert "多个可能的用户" in result.reason


def test_keeps_plain_monologue_unchanged():
    transcript = "这是没有说话人标签的一段完整口播。"

    result = transform_dialogue(transcript)

    assert result.status == "unchanged"
    assert result.text == transcript


def test_removes_orphaned_answer_fillers_after_host_turns_are_removed():
    transcript = (
        "【说话人1】：哪个庙呀？\n"
        "【说话人2】：也是在韶关那边，那个师傅推荐了这本书，我孩子因此改变了。\n"
        "【说话人1】：你觉得有帮助吗？\n"
        "【说话人2】：是的，孩子现在好多了。"
    )

    result = transform_dialogue(transcript)

    assert result.status == "auto"
    assert result.text.startswith("用户：在韶关那边，那个师傅推荐了这本书，我孩子因此改变了。\n孩子现在好多了。")
