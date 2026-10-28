"""The two settings the vendored engine reads. The bucket is only a name here: the local store
(aws_clients) ignores it and serves every key from folders on this machine."""


class _Settings:
    S3_BUCKET = "local"
    S3_REGION = "local"


settings = _Settings()
