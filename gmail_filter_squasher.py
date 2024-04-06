from collections import defaultdict
import json
import logging
import os.path

from google.auth.exceptions import GoogleAuthError
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build, Resource
from googleapiclient.errors import HttpError


# ------------------------------
# VARIABLES
# ------------------------------

# My recommendation would be to run it at least once in debug mode (DEBUG=True) to
# assert what filters will be squashed.
# Do note that no changes will be performed into Gmail until debug mode is manually
# deactivated (DEBUG=False).

# Debug mode
DEBUG = True
# File with the credentials to use
CREDENTIALS_FILE = "credentials.json"
# Verbosity mode. If True, extra logs will be output
VERBOSE_MODE = True

# File that will be used to store the access and refresh tokens once retrieved
TOKENS_FILE = "tokens.json"


# ------------------------------
# CODE
# ------------------------------

# Setup custom logging
logger = logging.getLogger(__name__)
ch = logging.StreamHandler()
formatter = logging.Formatter("[%(levelname)s] %(message)s")

logger.setLevel(logging.DEBUG if VERBOSE_MODE else logging.INFO)
ch.setFormatter(formatter)
logger.addHandler(ch)


class hashabledict(dict):
    def __hash__(self) -> int:
        return hash(frozenset(self))


def get_credentials() -> Credentials:
    """Obtain credentials to login into Google. It will look for a CREDENTIALS_FILE
    file with valid credentials. Once acquired, it will generate a TOKENS_FILE file
    with access and refresh tokens, which will attempt to use on subsequent calls.

    Returns:
        Credentials: Google credentials
    """
    creds = None
    scopes = ["https://www.googleapis.com/auth/gmail.settings.basic"]
    skip_token_storage = False

    # The file TOKENS_FILE stores the user's access and refresh tokens, and is
    # created automatically when the authorization flow completes for the first
    # time
    if os.path.exists(TOKENS_FILE):
        try:
            creds = Credentials.from_authorized_user_file(TOKENS_FILE, scopes)
        except (json.decoder.JSONDecodeError, ValueError):
            # The file exists but contains unusable credentials. We can authenticate
            # manually and avoid storing the new credentials, as that would overwrite
            # an existing file
            logger.warning(
                'Token file "%s" exists but does not contain valid authentication credentials.',
                TOKENS_FILE,
            )
            logger.warning(
                "Authentication will proceed, but manual intervention will be required on every subsequent execution."
            )
            logger.warning(
                'To prevent this, either change the value of `TOKENS_FILE` to a non-existent file, or remove the existing file at "%s"',
                TOKENS_FILE,
            )
            skip_token_storage = True

    # If there are no (valid) credentials available, let the user log in
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            # If there are credentials just expired, refresh them
            creds.refresh(Request())
        else:
            # If there are no credentials, obtain them
            flow = InstalledAppFlow.from_client_secrets_file(CREDENTIALS_FILE, scopes)
            creds = flow.run_local_server(port=0)

        if not skip_token_storage:
            # Save the credentials for the next run unless the file already existed, in
            # which case we prefer to avoid overwriting it
            with open(TOKENS_FILE, "w") as token:
                token.write(creds.to_json())
    return creds


def squash_filter(
    service: Resource, action: dict, criterias: list[dict]
) -> tuple[int, int]:
    """Recieve a series of criterias that all apply the same action and attempt to
    merge them into a single criteria.

    Args:
        service (Resource): A resource to interact with the Google API.
        action (dict): The action of the filter.
        criterias (list[dict]): The list of criterias that share the same action.

    Returns:
        tuple[int, int]: How many filters were created and how many were deleted.
    """
    logger.debug("These criterias: %s", criterias)
    logger.debug("Trigger the following action: %s", action)

    conditions = []
    original_filters = []
    created_filters = deleted_filters = 0

    for filter in criterias:
        if list(filter["criteria"]) == ["from"]:
            # We only want to squash those criterias formed by a single "from" clause
            conditions += [filter["criteria"]["from"]]
            original_filters += [filter["id"]]

    if len(conditions) > 1:
        # If there were multiple criterias formed strictly by a single "form" clause,
        # they can be merged into a single one.
        new_filter = {
            "criteria": {"from": " OR ".join(conditions)},
            "action": action,
        }
        logger.info("Creating new filter: %s", new_filter)

        if not DEBUG:
            # Create a new filter with the squashed criterias and the shared action
            try:
                results = (
                    service.users()
                    .settings()
                    .filters()
                    .create(userId="me", body=new_filter)
                    .execute()
                )
            except HttpError as error:
                logger.error(
                    "An error occurred when creating the new filter: %s", error
                )
                logger.error(
                    "Terminating the process to avoid leaving an unstable system."
                )
                raise
            else:
                logger.info("Created filter %s", results["id"])

        created_filters = 1

        for filter_id in original_filters:
            # Delete all the filters containing the criterias that were included in the
            # new squashed filter
            logger.info("Deleting filter %s...", filter_id)
            if not DEBUG:
                # Delete the filter
                try:
                    service.users().settings().filters().delete(
                        userId="me", id=filter_id
                    ).execute()

                except HttpError as error:
                    logger.error("An error occurred when deleting a filter: %s", error)
                    logger.error(
                        (
                            "Bear in mind that the squashed filter has already been "
                            "created so functionality remains the same. But you might "
                            "want to manually delete the remaining filters."
                        )
                    )
                    raise
                else:
                    logger.info("Deleted")
            deleted_filters += 1

        created_filters = 1

    else:
        logger.debug("Filters couldn't be squashed")

    logger.debug("---------------")

    return created_filters, deleted_filters


def main():

    if DEBUG:
        logger.info("RUNNING IN DEBUG MODE. NO CHANGES WILL BE APPLIED.")

    # Obtain Google credentials
    creds = get_credentials()
    squashed_filters = defaultdict(list)
    created_filters = deleted_filters = 0

    try:
        # Call the Gmail API to retrieve all the existing filters
        service = build("gmail", "v1", credentials=creds)
        results = service.users().settings().filters().list(userId="me").execute()
        filters = results.get("filter", [])
    except GoogleAuthError as e:
        logger.error(
            "An error occurred when attempting to use the provided credentials: %s", e
        )
        exit(1)
    except HttpError as e:
        logger.error(
            "An error occurred when attempting to retrieve the existing Gmail filters: %s",
            e,
        )
        exit(1)

    if not filters:
        logger.warning("No filters found.")
        return

    for filter in filters:
        # We want to merge all the filters according to their actions.
        # This will iterate all filters and build a dict where the action is the key
        # and the value, a list of filters (id, criteria) with the same action
        action = filter["action"]
        del filter["action"]
        squashed_filters[hashabledict(action)] += [filter]

    try:
        for action in squashed_filters:
            if len(squashed_filters[action]) > 1:
                # For each of the generated key-value pairs, we want to squash those
                # that have more than one possible criteria (hence, a value with a
                # length greater than 1)
                created, deleted = squash_filter(
                    service, action, squashed_filters[action]
                )

                created_filters += created
                deleted_filters += deleted

    except HttpError as e:
        logger.error("An error occurred: %s", e)
        logger.error("Terminating process.")
        exit(1)

    # Log the results
    if created_filters > 0:
        logger.info(
            "Squashed %d filters into %d new filters.", deleted_filters, created_filters
        )
    else:
        logger.info("No filters were squashed.")


if __name__ == "__main__":
    main()
