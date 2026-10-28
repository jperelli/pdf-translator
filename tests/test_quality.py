from app.languages import get_language
from app.models import Fit
from app.quality.checks import check_block, score

FIT = Fit(status="fit")
ES = get_language("es")


def cats(issues):
    return {(i.severity, i.category) for i in issues}


def test_brand_translated_is_critical():
    issues = check_block("Ride the Stark VARG", "Conduce la Fuerte VARG", ES, ["Stark", "VARG"], FIT, 0.72)
    assert ("critical", "terminology") in cats(issues)
    assert score(issues) <= 75


def test_missing_number_is_major_and_locale_separators_ok():
    assert ("major", "omission") in cats(check_block("Torque 938 Nm", "Par motor Nm", ES, [], FIT, 0.72))
    assert not check_block("Battery 7.2 kWh, 1,000 cycles", "Batería 7,2 kWh, 1.000 ciclos", ES, [], FIT, 0.72)


def test_wrong_script_and_untranslated():
    zh = get_language("zh")
    assert ("critical", "untranslated") in cats(check_block("Check the brakes", "Check the brakes", zh, [], FIT, .72))
    assert ("major", "untranslated") in cats(
        check_block("Check the brakes before riding", "Check the brakes before riding", ES, [], FIT, .72))
    assert not check_block("STARK FUTURE", "STARK FUTURE", zh, ["STARK", "FUTURE"], FIT, .72)


def test_layout_issues():
    assert ("major", "layout") in cats(check_block("Start", "Starten", ES, [], Fit(status="overflow", scale=0.5), .72))
    assert ("minor", "layout") in cats(check_block("Start", "Starten", ES, [], Fit(status="shrunk", scale=0.76), .72))
    assert not check_block("Start", "Iniciar", ES, [], Fit(status="shrunk", scale=0.94), .72)


def test_markup_mismatch_is_minor():
    assert ("minor", "markup") in cats(check_block("<b>Clean</b> it", "Límpialo", ES, [], FIT, .72))
