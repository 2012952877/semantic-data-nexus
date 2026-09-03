from .models import ParameterType, PhysicalSourceFragment, StatementParameter


def demo_sales_fragment(region: str | None) -> PhysicalSourceFragment:
    return PhysicalSourceFragment(
        source_name="demo_sales",
        sql=(
            "SELECT region, SUM(amount) AS total_amount "
            "FROM orders WHERE region = :region GROUP BY region ORDER BY region"
        ),
        parameters=(
            StatementParameter(name="region", type=ParameterType.STRING, value=region),
        ),
    )
