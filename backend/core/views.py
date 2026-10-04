from django.db import IntegrityError, connection, transaction
from django.db.models import Count
from rest_framework import viewsets
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from .models import ClothRoll, DipRun, Loft
from .serializers import ClothRollSerializer, DipRunSerializer, LoftSerializer


class LoftViewSet(viewsets.ModelViewSet):
    queryset = Loft.objects.annotate(roll_count=Count("rolls")).all()
    serializer_class = LoftSerializer


class ClothRollViewSet(viewsets.ModelViewSet):
    serializer_class = ClothRollSerializer

    def get_queryset(self):
        qs = ClothRoll.objects.select_related("loft").all()
        loft_id = self.request.query_params.get("loftId")
        status = self.request.query_params.get("status")
        if loft_id:
            qs = qs.filter(loft_id=loft_id)
        if status:
            qs = qs.filter(status=status)
        return qs


class DipRunViewSet(viewsets.ModelViewSet):
    serializer_class = DipRunSerializer
    http_method_names = ["get", "post", "head", "options"]

    def get_queryset(self):
        qs = DipRun.objects.select_related("roll", "roll__loft").all()
        roll_id = self.request.query_params.get("rollId")
        if roll_id:
            qs = qs.filter(roll_id=roll_id)
        return qs

    def create(self, request, *args, **kwargs):
        """
        同一卷只允许一版浸渍记录。

        面板连点（含历史遗留的重复请求）或两名浸胶工几乎同时给同一卷
        写入时，均在该卷行锁内以 update_or_create 落库：后写覆盖先写，
        最终只保留最后一次提交的合法记录。
        非正树脂在序列化器阶段被中文挡下，不会进入此处、不会入库。
        """
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        roll = serializer.validated_data["roll"]
        defaults = {
            "started_at": serializer.validated_data["started_at"],
            "resin_pct": serializer.validated_data["resin_pct"],
            "cure_hours": serializer.validated_data.get("cure_hours"),
            "notes": serializer.validated_data.get("notes", ""),
        }
        with transaction.atomic():
            roll_qs = ClothRoll.objects
            if connection.vendor == "postgresql":
                # PostgreSQL：锁住布卷行，串行化同卷的交叉写入（两名浸胶工同写一卷）
                roll_qs = roll_qs.select_for_update()
            locked_roll = roll_qs.get(pk=roll.pk)
            try:
                with transaction.atomic():
                    instance, _created = DipRun.objects.update_or_create(
                        roll=locked_roll, defaults=defaults
                    )
            except IntegrityError:
                # 唯一约束兜底（无行锁的数据库/极端并发）：首写已提交，则更新该唯一记录
                instance = DipRun.objects.get(roll=locked_roll)
                for field, value in defaults.items():
                    setattr(instance, field, value)
                instance.save()

        out = self.get_serializer(instance)
        return Response(out.data, status=201, headers=self.get_success_headers(out.data))


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def dashboard_stats(request):
    data = {
        "loftCount": Loft.objects.count(),
        "rawRollCount": ClothRoll.objects.filter(status=ClothRoll.STATUS_RAW).count(),
        "dippingRollCount": ClothRoll.objects.filter(
            status=ClothRoll.STATUS_DIPPING
        ).count(),
        "curedRollCount": ClothRoll.objects.filter(status=ClothRoll.STATUS_CURED).count(),
        "dipRunCount": DipRun.objects.count(),
    }
    return Response(data)
