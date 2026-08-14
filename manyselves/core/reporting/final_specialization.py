"""Chapter-specific lenses for the shared final-auditor Skill.

The template-derived final-auditor Skill remains one complete, reusable audit
method.  These task lenses specialize what that method must examine in each
Chief-owned chapter without creating three duplicated auditor Skills.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class FinalLaneSpecialization:
    chapter_id: str
    title: str
    review_focus: tuple[str, ...]

    def prompt_context(self) -> str:
        focus = "\n".join(f"- {item}" for item in self.review_focus)
        return (
            f'<final_lane_specialization chapter_id="{self.chapter_id}">\n'
            f"审查范围：Chapter {self.chapter_id} {self.title}\n"
            "继续使用完整 final-auditor Skill 的通用验收方法；以下问题只特化本章的"
            "审查重点，不创建新的写作方法，也不扩大 required_section_ids：\n"
            f"{focus}\n"
            "接口上下文只用于发现本章应承担的失真、遗漏或矛盾，finding target 仍只能"
            "属于本章。\n"
            "</final_lane_specialization>"
        )


FINAL_LANE_SPECIALIZATIONS: dict[str, FinalLaneSpecialization] = {
    "1": FinalLaneSpecialization(
        chapter_id="1",
        title="配电评估概述",
        review_focus=(
            "核验 1.1 是否准确说明评估范围、证据边界、限制和阅读前提，且不把未验证信息写成既定事实。",
            "核验 1.2 是否忠实概括五个批准模块的主要发现、风险轻重和判断依据，避免遗漏、夸大或平均化专业差异。",
            "核验 1.3 是否只按有证据的区域或责任边界组织重点、优先行动和验证状态，并与第三章行动结论保持一致。",
        ),
    ),
    "3": FinalLaneSpecialization(
        chapter_id="3",
        title="结论与建议",
        review_focus=(
            "核验 3.1.1 是否从批准成果归纳主要风险、根因和传播影响，而不是重复五个模块摘要。",
            "核验 3.1.2 与 3.1.3 是否分别完成专业维度比较和数据缺口分析，并说明其对判断可靠性与补证优先级的影响。",
            "核验 3.2 是否把风险转化为有责任接口、前置依赖、实施顺序、验收方法和剩余风险的行动包，并与第一章及实际存在的第四章一致。",
        ),
    ),
    "4": FinalLaneSpecialization(
        chapter_id="4",
        title="专项问题分析",
        review_focus=(
            "核验全部且仅有 special_topic_plan 声明的 4.n 小标题，标题、数量、顺序和逐节要求完全一致。",
            "核验每个专项小节形成自足的项目事实、分析判断、方案条件、权衡和验证方法，而不是指向其他章节的空壳。",
            "核验项目 Evidence、带来源的 Knowledge 与模型通用知识边界清楚，且专项结论不与第一章或第三章冲突。",
        ),
    ),
}


def final_lane_specialization(chapter_id: str) -> FinalLaneSpecialization:
    try:
        return FINAL_LANE_SPECIALIZATIONS[chapter_id]
    except KeyError as exc:
        raise ValueError(f"unsupported Final chapter: {chapter_id}") from exc
