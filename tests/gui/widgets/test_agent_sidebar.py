from manyselves.gui.widgets.agent_sidebar import AgentSidebar


def test_sidebar_lists_runtime_agents_and_emits_selection(qtbot):
    sidebar = AgentSidebar()
    qtbot.addWidget(sidebar)
    selected: list[str] = []
    sidebar.agent_selected.connect(selected.append)

    runtime_id = "module-2.4-specialist--session-abcd1234"
    sidebar.ensure_agent("main")
    sidebar.ensure_agent(runtime_id, "thinking")

    assert sidebar.agent_ids() == ["main", runtime_id]
    assert sidebar._rows[runtime_id].name.text() == "Module 2.4 Specialist"
    assert "Thinking" in sidebar._rows[runtime_id].status.text()

    sidebar.select_agent(runtime_id)
    assert selected[-1] == runtime_id


def test_sidebar_updates_agent_runtime_status(qtbot):
    sidebar = AgentSidebar()
    qtbot.addWidget(sidebar)
    runtime_id = "report-renderer--session-12345678"

    sidebar.ensure_agent(runtime_id)
    sidebar.set_agent_status(runtime_id, "running_tool")

    assert sidebar._rows[runtime_id].name.text() == "Render"
    assert "Using tool" in sidebar._rows[runtime_id].status.text()


def test_sidebar_labels_reporting_lane_identities_instead_of_generic_roles(qtbot):
    sidebar = AgentSidebar()
    qtbot.addWidget(sidebar)
    expected = {
        "module-auditor-2.1--session-a1b2c3d4": "Module 2.1 Auditor",
        "cross-owner-2.3--session-a1b2c3d4": "Cross 2.3 Owner",
        "chief-chapter-4--session-a1b2c3d4": "Chief Chapter 4",
        "final-chapter-3--session-a1b2c3d4": "Final Chapter 3 Auditor",
    }

    for runtime_id, label in expected.items():
        sidebar.ensure_agent(runtime_id)
        assert sidebar._rows[runtime_id].name.text() == label


def test_sidebar_shows_and_clears_current_agent_task(qtbot):
    sidebar = AgentSidebar()
    qtbot.addWidget(sidebar)
    runtime_id = "module-2.4-specialist--session-12345678"

    sidebar.ensure_agent(runtime_id, "thinking")
    sidebar.set_agent_task(runtime_id, "分析设备台账与缺失证据")

    assert "分析设备台账" in sidebar._rows[runtime_id].status.text()
    sidebar.set_agent_status(runtime_id, "idle")
    assert "分析设备台账" not in sidebar._rows[runtime_id].status.text()


def test_sidebar_can_switch_to_a_new_run_without_deleting_main(qtbot):
    sidebar = AgentSidebar()
    qtbot.addWidget(sidebar)
    old_runtime = "module-2.4-specialist--session-old"
    workflow_agent = "workflow-coordinator--session-new"
    sidebar.ensure_agent("main")
    sidebar.ensure_agent(workflow_agent)
    sidebar.ensure_agent(old_runtime)

    sidebar.retain_agents({"main", workflow_agent})

    assert sidebar.agent_ids() == ["main", workflow_agent]
    assert old_runtime not in sidebar._rows
