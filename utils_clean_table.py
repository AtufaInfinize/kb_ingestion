import boto3

REGION = "us-east-1"
TABLE_NAME = "url-registry-dev"
UNIVERSITY_ID = "gmu"

dynamodb = boto3.resource("dynamodb", region_name=REGION)
table = dynamodb.Table(TABLE_NAME)

STATUSES = ["pending", "crawled", "error", "failed", "dead",
            "redirected", "blocked_robots", "skipped_depth"]

def clear_university(table, university_id):
    items_deleted = 0

    for status in STATUSES:
        last_key = None
        while True:
            kwargs = {
                "IndexName": "university-status-index",
                "KeyConditionExpression": "university_id = :uid AND crawl_status = :status",
                "ExpressionAttributeValues": {":uid": university_id, ":status": status},
                "ProjectionExpression": "#u",
                "ExpressionAttributeNames": {"#u": "url"},
            }
            if last_key:
                kwargs["ExclusiveStartKey"] = last_key

            response = table.query(**kwargs)
            items = response.get("Items", [])

            if items:
                print(f"Deleting {len(items)} items with status={status}")
                with table.batch_writer() as batch:
                    for item in items:
                        batch.delete_item(Key={"url": item["url"]})
                        items_deleted += 1

            last_key = response.get("LastEvaluatedKey")
            if not last_key:
                break

    print(f"Deleted {items_deleted} total GMU items from {table.name}")

if __name__ == "__main__":
    clear_university(table, UNIVERSITY_ID)