from decimal import Decimal

from django.db import migrations, models


def clean_dip_runs(apps, schema_editor):
    """
    加约束前清理历史脏数据：
    1. 树脂百分比 <= 0（0 或负数）的浸渍记录直接删除；
    2. 同一卷有多条浸渍（连点/交叉写入产生的重复）时，
       只保留最近一条（started_at 最大，并列时 id 最大）。
    """
    DipRun = apps.get_model("core", "DipRun")

    DipRun.objects.filter(resin_pct__lte=Decimal("0")).delete()

    duplicate_roll_ids = (
        DipRun.objects.values("roll_id")
        .annotate(count_id=models.Count("id"))
        .filter(count_id__gt=1)
        .values_list("roll_id", flat=True)
    )
    for roll_id in duplicate_roll_ids:
        latest = (
            DipRun.objects.filter(roll_id=roll_id)
            .order_by("-started_at", "-id")
            .first()
        )
        if latest is not None:
            DipRun.objects.filter(roll_id=roll_id).exclude(pk=latest.pk).delete()


def noop_reverse(apps, schema_editor):
    # 脏数据与重复记录的删除不可逆
    pass


class Migration(migrations.Migration):

    dependencies = [
        ("core", "0001_initial"),
    ]

    operations = [
        migrations.RunPython(clean_dip_runs, noop_reverse),
        migrations.AddConstraint(
            model_name="diprun",
            constraint=models.UniqueConstraint(
                fields=("roll",), name="uniq_diprun_one_per_roll"
            ),
        ),
        migrations.AddConstraint(
            model_name="diprun",
            constraint=models.CheckConstraint(
                check=models.Q(("resin_pct__gt", Decimal("0"))),
                name="chk_diprun_resin_positive",
            ),
        ),
    ]
