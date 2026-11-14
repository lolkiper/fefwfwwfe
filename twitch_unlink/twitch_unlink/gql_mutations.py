DELETE_OAUTH_APP = """
mutation DeleteOauthApp($id: ID!) {
  deleteOauthApp(input: { id: $id }) {
    __typename
  }
}
"""