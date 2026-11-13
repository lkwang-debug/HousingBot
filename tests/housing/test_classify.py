from __future__ import annotations

from app.housing.classify import classify


def test_rent_launch_is_pushable():
    result = classify(
        "光明区住房和建设局关于2026年第一批次面向个人配租保障性租赁住房的通告（住保租〔2026〕038号）"
    )
    assert result.category == "rent_launch"
    assert result.housing_types == ["baozu"]
    assert result.is_push_candidate is True


def test_sale_notice_is_excluded():
    result = classify("深圳市住房和建设局关于安居型商品房认购有关事项的通告")
    assert result.category == "excluded"
    assert result.is_target is False
    assert result.is_push_candidate is False


def test_procurement_is_not_target():
    result = classify("沙湾河深圳水库截排二期工程自行采购结果公告")
    assert result.category == "procurement"
    assert result.is_target is False


def test_process_beats_vacancy_and_launch():
    # “剩余房源+认租资格核查结果”必须落在过程公示，不能当成腾退或配租启动。
    result = classify("关于剩余房源认租资格核查结果的通告")
    assert result.category == "process"


def test_process_without_housing_type_is_not_target():
    result = classify("关于某工程结算审核结果的公示")
    assert result.category == "process"
    assert result.is_target is False


def test_vacancy_exit_is_pushable():
    result = classify(
        "罗湖区2026年第三季度保障性住房退出信息一览表（更新至2026年9月30日）",
        body="现将本季度腾退房源情况公布如下",
    )
    assert result.category == "vacancy_exit"
    assert result.is_push_candidate is True


def test_body_only_vacancy_requires_strong_phrase():
    result = classify(
        "关于2026年公租房轮候申请受理的通告",
        body="申请家庭退出轮候的，应当提交书面材料",
    )
    assert result.category != "vacancy_exit"


def test_delivery_notice_is_noise():
    result = classify(
        "关于向张某送达《深圳市罗湖区住房和建设局协助调查通知书》的公告",
        body="涉及公共租赁住房",
    )
    assert result.is_target is False
    assert result.exclude_reason == "noise"


def test_lottery_result_is_pushable():
    result = classify("坪山区住房保障中心关于2026年第一批次面向个人配租保障性租赁住房公证摇号结果的通告")
    assert result.category == "lottery_result"
    assert result.is_push_candidate is True


def test_selection_name_list_is_selection_not_lottery_result():
    # 选房名单属于选房阶段；当成摇号结果会把标题写成"公证摇号结果已公布"。
    result = classify(
        "罗湖区住房和建设局关于2025年第二批面向辖区户籍在册轮候家庭配租公共租赁住房选房名单及选房签约有关事项的通告"
    )
    assert result.category == "process"
    assert result.stage == "selection"
    assert result.is_push_candidate is True


def test_selection_result_is_process_not_lottery():
    # 选房结果 = 谁选走了房，不是排位名单，不能当摇号结果推；
    # 但它仍是批次流程的一步，按"选房"节点推一条进度消息。
    result = classify(
        "深圳市福田区住房和建设局关于2025年第二批配租公共租赁住房选房结果公示",
        "本批次为公共租赁住房",
    )
    assert result.category == "process"
    assert result.stage == "selection"
    assert result.is_push_candidate is True


def test_lifecycle_stages_are_pushable():
    # 认租→核查→摇号→选房→签约，每一步都要能即时推，不能等早报。
    cases = {
        "光明区2026年第一批次保障性租赁住房认租资格核查结果的通告": "review",
        "光明区2026年第一批次保障性租赁住房公证摇号结果的通告": None,  # lottery_result 单独一类
        "光明区2026年第一批次保障性租赁住房选房有关事项的通告": "selection",
        "光明区2026年第一批次保障性租赁住房选房签约工作的通告": "selection",
        "光明区2026年第一批次保障性租赁住房签约有关事项的通告": "signing",
    }
    for title, stage in cases.items():
        result = classify(title, "保障性租赁住房")
        assert result.is_push_candidate is True, title
        if stage:
            assert result.category == "process"
            assert result.stage == stage, title
        else:
            assert result.category == "lottery_result"


def test_generic_process_notice_stays_off_instant_push():
    result = classify("光明区2026年第一批次保障性租赁住房续租的通告", "保障性租赁住房")
    assert result.category == "process"
    assert result.stage is None
    assert result.is_push_candidate is False


def test_household_allocation_result_is_process():
    result = classify(
        "坪山区住房保障中心关于面向企（事）业单位配租保障性租赁住房分配结果公示及选房签约的通告"
    )
    assert result.category == "process"


def test_housing_type_from_body_when_title_is_generic():
    result = classify("关于公布2026年配租结果的通知", body="本次配租对象为公共租赁住房轮候家庭")
    assert result.housing_types == ["gongzu"]
