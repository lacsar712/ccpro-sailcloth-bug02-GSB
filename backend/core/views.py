import time

from django.db import IntegrityError, OperationalError, transaction
from django.db.models import Count
from rest_framework import viewsets
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from .models import ClothRoll, DipRun, Loft
from .serializers import ClothRollSerializer, DipRunSerializer, LoftSerializer

# 交叉提交撞唯一约束（或 SQLite 等库级写锁）时的重放次数。
_WRITE_MAX_ATTEMPTS = 8


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

    def perform_create(self, serializer):
        # 同一布卷只保留一版浸渍记录：锁住布卷行后顶替写入。
        # 右侧面板连点、两名浸胶工交叉提交都会在此串行，最后一次合法提交生效。
        validated = serializer.validated_data
        roll_pk = validated["roll"].pk
        defaults = {
            "started_at": validated["started_at"],
            "resin_pct": validated["resin_pct"],
            "cure_hours": validated.get("cure_hours"),
            "notes": validated.get("notes", ""),
        }

        last_error = None
        for attempt in range(_WRITE_MAX_ATTEMPTS):
            try:
                with transaction.atomic():
                    # Postgres：行锁让同卷并发写严格串行，唯一约束永不冲突。
                    roll = ClothRoll.objects.select_for_update().get(pk=roll_pk)
                    instance, _ = DipRun.objects.update_or_create(
                        roll=roll, defaults=defaults
                    )
                serializer.instance = instance
                return
            except (IntegrityError, OperationalError) as exc:
                # SQLite 不支持行锁（select_for_update 为空操作），
                # 交叉提交可能直接撞唯一约束或库级写锁，短暂退避后重放。
                last_error = exc
                time.sleep(0.02 * (attempt + 1))
        raise last_error


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
