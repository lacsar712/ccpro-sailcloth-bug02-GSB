from decimal import Decimal

from django.db import migrations, models


def dedupe_dip_runs(apps, schema_editor):
    """收敛历史脏数据：非正树脂记录删除；每卷只留最新一条浸渍。"""
    DipRun = apps.get_model("core", "DipRun")

    # 1) 非法（0 / 负数）树脂记录本就不该入库，清掉。
    DipRun.objects.filter(resin_pct__lte=Decimal("0")).delete()

    # 2) 同一卷仍有多条时，保留 started_at/id 最新的一条，删除其余。
    seen_rolls = set()
    for pk, roll_id in DipRun.objects.order_by("-started_at", "-id").values_list(
        "id", "roll_id"
    ):
        if roll_id in seen_rolls:
            DipRun.objects.filter(pk=pk).delete()
        else:
            seen_rolls.add(roll_id)


def noop(apps, schema_editor):
    pass


class Migration(migrations.Migration):
    dependencies = [
        ("core", "0001_initial"),
    ]

    operations = [
        migrations.RunPython(dedupe_dip_runs, noop),
        migrations.AddConstraint(
            model_name="diprun",
            constraint=models.UniqueConstraint(
                fields=("roll",), name="uniq_dip_run_per_roll"
            ),
        ),
    ]
